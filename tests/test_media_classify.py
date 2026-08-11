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


class EnrichTaskMediaMetadataTest(unittest.TestCase):
    def _store(self, tmp):
        from pathlib import Path

        from app.task_store import TaskStore

        return TaskStore(Path(tmp) / "tasks.db")

    def test_backfills_poster_and_persists_for_succeeded_task(self):
        import json
        import tempfile
        from pathlib import Path

        from app.models import TaskStage, TaskStatus

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            task = store.upsert_task("backfill", "", "https://115cdn.com/s/backfill")
            store.patch_metadata(task.id, {
                "tmdb_id": "10974",
                "type": "movie",
                "category": "欧美电影",
                "title": "龙兄虎弟",
            })
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            class FakeResolver:
                enabled = True

                def lookup(self, tmdb_id, media_type, name):
                    return {
                        "ok": True,
                        "poster_path": "/i9zrfkod6qM3CWvNUmllJ104K7g.jpg",
                        "overview": "简介",
                        "genres": ["冒险", "动作", "喜剧"],
                        "vote_average": 7.0,
                        "release_date": "1986-08-16",
                    }

            serialized = [{
                "id": task.id,
                "status": "succeeded",
                "metadata": dict(store.find_task(task.id).metadata),
            }]
            from app.media.classify import enrich_task_media_metadata

            result = enrich_task_media_metadata(store, serialized, FakeResolver())

            self.assertEqual(result[0]["metadata"]["poster_path"], "/i9zrfkod6qM3CWvNUmllJ104K7g.jpg")
            self.assertEqual(result[0]["metadata"]["vote_average"], 7.0)
            # persisted so a later refresh is a no-op
            persisted = store.find_task(task.id).metadata
            self.assertEqual(persisted["poster_path"], "/i9zrfkod6qM3CWvNUmllJ104K7g.jpg")
            self.assertEqual(persisted["release_date"], "1986-08-16")

    def test_skips_tasks_with_poster_or_no_tmdb_id(self):
        import tempfile

        from app.media.classify import enrich_task_media_metadata

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            task = store.upsert_task("skip", "", "https://115cdn.com/s/skip")
            store.patch_metadata(task.id, {"poster_path": "/have.jpg", "tmdb_id": "1", "type": "movie"})
            task2 = store.upsert_task("notmdb", "", "https://115cdn.com/s/notmdb")
            store.patch_metadata(task2.id, {"category": "欧美电影", "type": "movie"})

            class BoomResolver:
                enabled = True

                def lookup(self, *args):
                    raise AssertionError("should not be called")

            serialized = [
                {"id": task.id, "metadata": dict(store.find_task(task.id).metadata)},
                {"id": task2.id, "metadata": dict(store.find_task(task2.id).metadata)},
            ]
            enrich_task_media_metadata(store, serialized, BoomResolver())
            # resolver never called -> no exception proves skip

    def test_limits_enrichment_per_call(self):
        import tempfile

        from app.media.classify import enrich_task_media_metadata

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for idx in range(5):
                task = store.upsert_task(f"lim{idx}", "", f"https://115cdn.com/s/lim{idx}")
                store.patch_metadata(task.id, {"tmdb_id": str(100 + idx), "type": "movie"})
            calls = []

            class CountingResolver:
                enabled = True

                def lookup(self, tmdb_id, media_type, name):
                    calls.append(tmdb_id)
                    return {"ok": True, "poster_path": "/p.jpg", "genres": [], "vote_average": None, "release_date": ""}

            tasks = store.list_recent_tasks(limit=10)
            serialized = [{"id": t.id, "metadata": dict(t.metadata)} for t in tasks]
            enrich_task_media_metadata(store, serialized, CountingResolver(), max_enrich=2)
            self.assertEqual(len(calls), 2)

    def test_failed_lookup_does_not_crash(self):
        import tempfile

        from app.media.classify import enrich_task_media_metadata

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            task = store.upsert_task("fail", "", "https://115cdn.com/s/fail")
            store.patch_metadata(task.id, {"tmdb_id": "999999", "type": "movie"})

            class FailResolver:
                enabled = True

                def lookup(self, *args):
                    raise RuntimeError("tmdb down")

            serialized = [{"id": task.id, "metadata": dict(store.find_task(task.id).metadata)}]
            result = enrich_task_media_metadata(store, serialized, FailResolver())
            self.assertEqual(result[0]["metadata"].get("poster_path", ""), "")


if __name__ == "__main__":
    unittest.main()
