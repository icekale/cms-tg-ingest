import unittest

from app.media.classify import (
    extract_tmdb_search_query,
    extract_year_from_name,
    has_indian_movie_hint,
)


class HasIndianMovieHintTest(unittest.TestCase):
    def test_explicit_markers_still_match(self):
        for name in ("宝莱坞.大片", "bollywood.movie.2021", "Hindi.Dubbed", "Tamil.2023"):
            self.assertTrue(has_indian_movie_hint(name), name)

    def test_common_english_words_are_not_flagged(self):
        # "ta"/"te"/"hi" substrings collide with common English titles; a
        # plain substring match previously misclassified these as Indian.
        for name in (
            "Titanic.1997.1080p",
            "千与千寻.Spirited.Away.2001",
            "Person.of.Interest.S01E01.1080p",
            "The.Terminator.1984",
            "Interstellar.2014",
        ):
            self.assertFalse(has_indian_movie_hint(name), name)

    def test_two_letter_markers_match_as_standalone_words(self):
        self.assertTrue(has_indian_movie_hint("Hi.Nanna.2023.1080p"))


class ExtractTmdbSearchQueryTest(unittest.TestCase):
    def test_sxx_eyy_series_title(self):
        self.assertEqual(
            extract_tmdb_search_query("Person.of.Interest.S01E01.1080p"),
            "Person of Interest",
        )
        self.assertEqual(
            extract_tmdb_search_query("The.Good.Doctor.S04E02.1080p"),
            "The Good Doctor",
        )

    def test_single_word_movie_title(self):
        self.assertEqual(extract_tmdb_search_query("Dune.2021.2160p"), "Dune")
        self.assertEqual(extract_tmdb_search_query("Joker.2019.2160p"), "Joker")

    def test_title_containing_year_keeps_both_words(self):
        # "2077" belongs to the title, not the release-year marker.
        self.assertEqual(
            extract_tmdb_search_query("Cyberpunk.2077.2020"),
            "Cyberpunk 2077",
        )

    def test_multi_word_series_title_still_works(self):
        self.assertEqual(
            extract_tmdb_search_query("House.of.the.Dragon.S02.2024.UHD.BluRay"),
            "House of the Dragon",
        )
        self.assertEqual(
            extract_tmdb_search_query("Greys.Anatomy.S22.1080p.DSNP"),
            "Greys Anatomy",
        )
        self.assertEqual(
            extract_tmdb_search_query("Le.Comte.de.Monte-Cristo.2024.2160p.mkv"),
            "Le Comte de Monte Cristo",
        )


class ExtractYearFromNameTest(unittest.TestCase):
    def test_resolution_dimension_is_not_a_year(self):
        self.assertEqual(extract_year_from_name("Movie.1920x1080.2160p.2020"), "2020")
        self.assertEqual(extract_year_from_name("Movie.1920x1080.2160p"), "")

    def test_title_year_is_not_treated_as_release_year(self):
        self.assertEqual(extract_year_from_name("Cyberpunk.2077.2020"), "2020")

    def test_plain_years_still_extracted(self):
        self.assertEqual(extract_year_from_name("Dune.2021.2160p"), "2021")
        self.assertEqual(extract_year_from_name("2001.A.Space.Odyssey.1968"), "1968")


if __name__ == "__main__":
    unittest.main()
