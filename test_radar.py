import unittest

from radar import (aggregate_signals, classify, classify_detail,
                   is_removed_horror_recommendation, parse_inkitt,
                   recommendation_constraints_met)


class RadarResearchTests(unittest.TestCase):
    def test_werewolf_requires_context(self):
        self.assertEqual(classify("Alpha Protocol"), "other")
        detail = classify_detail("The Alpha Bride", "A werewolf pack and its luna mate bond", ["Romance"])
        self.assertEqual(detail["genre"], "werewolf_shifter")
        self.assertGreaterEqual(detail["confidence"], 0.7)

    def test_cross_source_signal_records_reader_pools(self):
        signal = aggregate_signals([
            {"source": "royalroad", "rank": 1, "category": "litrpg", "title": "A"},
            {"source": "wattpad", "rank": 1, "category": "werewolf_shifter", "title": "B"},
            {"source": "inkitt", "rank": 3, "category": "werewolf_shifter", "title": "C"},
        ])
        self.assertEqual(signal["werewolf_shifter"]["sourceCount"], 2)
        self.assertEqual(signal["litrpg"]["sourceCount"], 1)

    def test_inkitt_jsonld_parser(self):
        html = '<script type="application/ld+json">{"itemListElement":[{"position":1,"name":"The Alpha Mate","description":"A wolf romance"}]}</script>'
        rows = parse_inkitt(html, 10)
        self.assertEqual(rows[0]["title"], "The Alpha Mate")
        self.assertEqual(rows[0]["rank"], 1)

    def test_horror_recommendation_is_removed(self):
        self.assertTrue(is_removed_horror_recommendation({
            "genre": "Horror Romance", "parent_genre": "Horror", "subgenres": ["Slasher"]
        }))
        self.assertFalse(is_removed_horror_recommendation({
            "genre": "Dark Romance", "parent_genre": "Romance", "subgenres": ["Mafia"]
        }))

    def test_constraints_accept_exact_five_two_one_mix(self):
        recs = [
            {"genre": "Werewolf Romance", "parent_genre": "Werewolf/Shifter", "adult_intensity": 3}
            for _ in range(5)
        ] + [
            {"genre": "Dark Romance", "parent_genre": "Dark Romance", "adult_intensity": 4},
            {"genre": "Mafia Dark Romance", "parent_genre": "Dark Romance", "adult_intensity": 5},
            {"genre": "LGBTQ+ Romance", "parent_genre": "LGBTQ+ Romance", "adult_intensity": 4},
        ]
        self.assertTrue(recommendation_constraints_met(recs, 8, "weighted"))

    def test_constraints_reject_horror_for_every_portfolio(self):
        recs = [{"genre": "Psychological Thriller", "parent_genre": "Thriller"}]
        self.assertFalse(recommendation_constraints_met(recs, 5, "natural"))

    def test_constraints_reject_wrong_werewolf_count(self):
        recs = [
            {"genre": "Werewolf Romance", "parent_genre": "Werewolf/Shifter", "adult_intensity": 3}
            for _ in range(4)
        ] + [
            {"genre": "Dark Romance", "parent_genre": "Dark Romance", "adult_intensity": 4},
            {"genre": "Dark Romance", "parent_genre": "Dark Romance", "adult_intensity": 5},
            {"genre": "Dark Romance", "parent_genre": "Dark Romance", "adult_intensity": 4},
            {"genre": "LGBTQ+ Romance", "parent_genre": "LGBTQ+ Romance", "adult_intensity": 4},
        ]
        self.assertFalse(recommendation_constraints_met(recs, 8, "weighted"))


if __name__ == "__main__":
    unittest.main()
