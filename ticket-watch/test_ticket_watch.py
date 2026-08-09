"""Offline checks for the parsing and diff logic (no network, no email)."""

from __future__ import annotations

import unittest

import ticket_watch as tw


JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@type": "Event", "name": "Tucker Wetmore",
 "offers": [
   {"@type": "Offer", "name": "General Admission", "price": "53.00",
    "availability": "https://schema.org/InStock"},
   {"@type": "Offer", "name": "The Patio", "price": 256,
    "availability": "https://schema.org/LimitedAvailability"},
   {"@type": "AggregateOffer", "lowPrice": 53, "highPrice": 256}
 ]}
</script>
</head><body>x</body></html>
"""

TEXT_ONLY_PAGE = "<html><body><div>From $53.00</div><div>up to $256.00</div></body></html>"


class ParsePrice(unittest.TestCase):
    def test_reads_numbers_strings_and_currency(self):
        self.assertEqual(tw.parse_price(53), 53.0)
        self.assertEqual(tw.parse_price("53.00"), 53.0)
        self.assertEqual(tw.parse_price("$1,256.50"), 1256.50)

    def test_rejects_implausible_and_non_numeric(self):
        for value in (None, True, "sold out", 0, 99_999):
            self.assertIsNone(tw.parse_price(value), value)

    def test_settings_are_not_clamped_to_the_ticket_range(self):
        self.assertEqual(tw.parse_setting("0.50"), 0.50)
        self.assertIsNone(tw.parse_setting(""))
        self.assertIsNone(tw.parse_setting(None))


class ExtractOffers(unittest.TestCase):
    def test_reads_json_ld_offers_with_labels_and_availability(self):
        offers = tw.extract_offers(JSON_LD_PAGE)
        self.assertEqual(offers["General Admission"].price, 53.0)
        self.assertEqual(offers["General Admission"].availability, "InStock")
        self.assertEqual(offers["The Patio"].price, 256.0)

    def test_aggregate_range_is_kept_separately_from_sections(self):
        offers = tw.extract_offers(JSON_LD_PAGE)
        self.assertIn("Ticket (from)", offers)
        self.assertIn("Ticket (to)", offers)
        self.assertEqual(offers["Ticket (to)"].price, 256.0)

    def test_falls_back_to_page_text_when_there_is_no_json(self):
        offers = tw.extract_offers(TEXT_ONLY_PAGE)
        self.assertEqual(offers["Listed price (from)"].price, 53.0)
        self.assertEqual(offers["Listed price (to)"].price, 256.0)

    def test_no_prices_yields_no_offers(self):
        self.assertEqual(tw.extract_offers("<html><body>TBA</body></html>"), {})


class DiffOffers(unittest.TestCase):
    def test_ignores_movement_below_the_threshold(self):
        self.assertEqual(tw.diff_offers({"GA": 53.0}, {"GA": 53.40}, 1.0), [])

    def test_reports_drops_first_then_rises(self):
        changes = tw.diff_offers(
            {"GA": 53.0, "Patio": 256.0}, {"GA": 60.0, "Patio": 200.0}, 1.0
        )
        self.assertEqual([change.label for change in changes], ["Patio", "GA"])
        self.assertEqual(changes[0].kind, "down")
        self.assertEqual(changes[1].kind, "up")

    def test_flags_added_and_removed_tiers(self):
        changes = tw.diff_offers({"GA": 53.0}, {"Patio": 256.0}, 1.0)
        kinds = {change.label: change.kind for change in changes}
        self.assertEqual(kinds, {"Patio": "new", "GA": "gone"})


class BlockDetection(unittest.TestCase):
    class FakeResponse:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

    def test_short_pages_errors_and_challenges_are_blocks(self):
        self.assertTrue(tw.looks_blocked(self.FakeResponse(403, "x" * 50_000)))
        self.assertTrue(tw.looks_blocked(self.FakeResponse(200, "tiny")))
        self.assertTrue(tw.looks_blocked(
            self.FakeResponse(200, "Just a moment..." + "x" * 50_000)))

    def test_a_real_page_is_not_a_block(self):
        self.assertFalse(tw.looks_blocked(self.FakeResponse(200, "ticket " * 10_000)))


class FailureWarnings(unittest.TestCase):
    def test_silent_until_the_streak_is_established(self):
        self.assertFalse(tw.should_warn(1))
        self.assertFalse(tw.should_warn(2))
        self.assertTrue(tw.should_warn(3))

    def test_then_about_daily_rather_than_every_run(self):
        # A blocked IP stays blocked; 4..47 must not each send an email.
        self.assertEqual([n for n in range(4, 100) if tw.should_warn(n)], [48, 96])


class Durations(unittest.TestCase):
    def test_formats_minutes_hours_and_days(self):
        self.assertEqual(tw.format_duration(45 * 60), "45m")
        self.assertEqual(tw.format_duration(6 * 3600 + 12 * 60), "6h 12m")
        self.assertEqual(tw.format_duration(3 * 86400 + 4 * 3600), "3d 4h")

    def test_held_for_handles_naive_stamps_and_junk(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(tw.held_for("2026-08-09T06:00:00", now), "6h 0m")
        self.assertEqual(tw.held_for("2026-08-09T06:00:00+00:00", now), "6h 0m")
        self.assertEqual(tw.held_for("not a date", now), "")
        self.assertEqual(tw.held_for(None, now), "")

    def test_stored_since_reads_only_well_formed_entries(self):
        state = {"offers": {"GA": {"price": 62.0, "since": "2026-08-09T06:00:00+00:00"},
                            "Patio": {"price": 256.0}, "Junk": "nope"}}
        self.assertEqual(tw.stored_since(state), {"GA": "2026-08-09T06:00:00+00:00"})


class EmailBodies(unittest.TestCase):
    def test_drop_email_names_the_drop_and_escapes_input(self):
        changes = [tw.Change("GA & Lawn <b>", 53.0, 40.0)]
        subject, body_html, body_text = tw.price_change_email(
            "Show", "https://example.test/e", changes, 40.0)
        self.assertIn("1 price drop(s)", subject)
        self.assertIn("GA &amp; Lawn &lt;b&gt;", body_html)
        self.assertNotIn("<b>", body_html)
        self.assertIn("$40.00", body_text)

    def test_held_column_shows_a_duration_or_a_dash(self):
        changes = [tw.Change("GA", 62.0, 55.0), tw.Change("Patio", 256.0, 275.0)]
        _, body_html, _ = tw.price_change_email(
            "Show", "https://example.test/e", changes, 55.0, {"GA": "6h 12m"})
        self.assertIn("<td>6h 12m</td>", body_html)
        self.assertIn("<td>&mdash;</td>", body_html)
        self.assertNotIn("&amp;mdash;", body_html)


if __name__ == "__main__":
    unittest.main()
