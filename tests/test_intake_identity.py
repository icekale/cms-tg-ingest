import unittest

from app.media.intake_identity import (
    cleanup_root_action,
    collect_file_ids_under_dest,
    dest_file_ids_from_hits,
    dest_id_from_file_hits,
    is_season_folder_name,
    is_video_name,
    snapshot_files,
)


class IntakeIdentitySnapshotTests(unittest.TestCase):
    def test_video_suffix_and_season_names(self):
        self.assertTrue(is_video_name("拆弹专家.2017.mkv"))
        self.assertFalse(is_video_name("拆弹专家.2017.chs.ass"))
        self.assertTrue(is_season_folder_name("Season 03"))
        self.assertTrue(is_season_folder_name("第3季"))
        self.assertFalse(is_season_folder_name("C-拆弹专家-2017-[tmdb=441531]"))

    def test_snapshot_lists_videos_two_levels_for_season_roots(self):
        listed = {
            "recv-folder": [
                {"fid": "share-should-ignore", "n": "poster.jpg"},
                {"cid": "season-1", "n": "Season 1", "pid": "recv-folder"},
                {"fid": "video-root", "n": "Extra.mkv", "cid": "recv-folder"},
            ],
            "season-1": [
                {"fid": "ep1", "n": "Show.S01E01.mkv", "cid": "season-1"},
                {"fid": "sub1", "n": "Show.S01E01.ass", "cid": "season-1"},
            ],
        }

        def list_files(parent_id, limit=500):
            return list(listed.get(str(parent_id), []))

        files = snapshot_files(
            [
                {"file_id": "recv-folder", "file_name": "Show", "is_folder": True},
            ],
            list_files,
        )
        self.assertEqual(
            {(item["id"], item["name"]) for item in files},
            {("video-root", "Extra.mkv"), ("ep1", "Show.S01E01.mkv")},
        )

    def test_snapshot_lists_videos_in_release_named_season_packs(self):
        listed = {
            "recv-dp": [
                {
                    "cid": "pack-s01",
                    "n": "D.P.S01.KOREAN.2160p.NF.WEB-DL.x265.10bit.HDR.DDP5.1-XEBEC[rartv]",
                    "pid": "recv-dp",
                },
                {
                    "cid": "pack-s02",
                    "n": "D.P.S02.2160p.NF.WEB-DL.DDP5.1.Atmos.DV.HDR10.H.265-APEX",
                    "pid": "recv-dp",
                },
            ],
            "pack-s01": [
                {"fid": "ep-s01e01", "n": "D.P.S01E01.mkv", "cid": "pack-s01"},
                {"fid": "nfo-s01", "n": "D.P.S01E01.nfo", "cid": "pack-s01"},
            ],
            "pack-s02": [
                {"fid": "ep-s02e01", "n": "D.P.S02E01.mkv", "cid": "pack-s02"},
            ],
        }

        def list_files(parent_id, limit=500):
            return list(listed.get(str(parent_id), []))

        files = snapshot_files(
            [
                {"file_id": "recv-dp", "file_name": "D.P：逃兵追缉令", "is_folder": True},
            ],
            list_files,
        )
        self.assertEqual(
            {(item["id"], item["name"]) for item in files},
            {("ep-s01e01", "D.P.S01E01.mkv"), ("ep-s02e01", "D.P.S02E01.mkv")},
        )

    def test_snapshot_skips_nested_folder_without_videos(self):
        listed = {
            "recv-folder": [
                {"cid": "screens", "n": "Screens", "pid": "recv-folder"},
                {"fid": "root-video", "n": "Movie.mkv", "cid": "recv-folder"},
            ],
            "screens": [
                {"fid": "shot", "n": "front.jpg", "cid": "screens"},
            ],
        }

        def list_files(parent_id, limit=500):
            return list(listed.get(str(parent_id), []))

        files = snapshot_files(
            [{"file_id": "recv-folder", "file_name": "Movie", "is_folder": True}],
            list_files,
        )
        self.assertEqual(files, [{"id": "root-video", "name": "Movie.mkv"}])

    def test_snapshot_single_video_root(self):
        files = snapshot_files(
            [{"file_id": "lone-mkv", "file_name": "Movie.mkv", "is_folder": False}],
            lambda *_args, **_kwargs: [],
        )
        self.assertEqual(files, [{"id": "lone-mkv", "name": "Movie.mkv"}])

    def test_snapshot_empty_folder_returns_empty_files(self):
        files = snapshot_files(
            [{"file_id": "empty-folder", "file_name": "Empty", "is_folder": True}],
            lambda *_args, **_kwargs: [],
        )
        self.assertEqual(files, [])

    def test_snapshot_raises_when_folder_list_fails(self):
        def list_files(parent_id, limit=500):
            raise RuntimeError("115 risk control")

        with self.assertRaises(RuntimeError):
            snapshot_files(
                [{"file_id": "recv-folder", "file_name": "Show", "is_folder": True}],
                list_files,
            )

    def test_snapshot_raises_when_season_list_fails(self):
        def list_files(parent_id, limit=500):
            if str(parent_id) == "recv-folder":
                return [{"cid": "season-1", "n": "Season 1", "pid": "recv-folder"}]
            raise RuntimeError("115 risk control")

        with self.assertRaises(RuntimeError):
            snapshot_files(
                [{"file_id": "recv-folder", "file_name": "Show", "is_folder": True}],
                list_files,
            )


class IntakeIdentityDestTests(unittest.TestCase):
    def test_groups_files_by_destination_and_walks_up_season_parents(self):
        result = dest_file_ids_from_hits(
            file_hits=[
                {"fid": "episode-a", "cid": "season-a"},
                {"fid": "episode-b", "cid": "season-b"},
            ],
            folder_hits=[
                {"fid": "season-a", "cid": "dest-a", "n": "Season 1"},
                {"fid": "season-b", "cid": "dest-b", "n": "Season 2"},
                {"fid": "dest-a", "cid": "library", "n": "Show A"},
                {"fid": "dest-b", "cid": "library", "n": "Show B"},
            ],
            expected_ids=["episode-a", "episode-b"],
        )
        self.assertEqual(
            result,
            {"dest-a": ["episode-a"], "dest-b": ["episode-b"]},
        )

    def test_grouping_empty_expected_ids_is_incomplete(self):
        result = dest_file_ids_from_hits(
            file_hits=[{"fid": "episode-a", "cid": "dest-a"}],
            folder_hits=[{"fid": "dest-a", "n": "Show A"}],
            expected_ids=[None, "  "],
        )
        self.assertEqual(result, {})

    def test_grouping_missing_expected_file_is_incomplete(self):
        result = dest_file_ids_from_hits(
            file_hits=[{"fid": "episode-a", "cid": "dest-a"}],
            folder_hits=[{"fid": "dest-a", "n": "Show A"}],
            expected_ids=["episode-a", "episode-b"],
        )
        self.assertEqual(result, {})

    def test_grouping_sorts_reversed_hits_deterministically(self):
        result = dest_file_ids_from_hits(
            file_hits=[
                {"fid": "episode-b", "cid": "dest-b"},
                {"fid": "episode-a", "cid": "dest-a"},
            ],
            folder_hits=[
                {"fid": "dest-b", "n": "Show B"},
                {"fid": "dest-a", "n": "Show A"},
            ],
            expected_ids=["episode-b", "episode-a"],
        )
        self.assertEqual(
            result,
            {"dest-a": ["episode-a"], "dest-b": ["episode-b"]},
        )

    def test_grouping_duplicate_file_hit_same_destination_is_stable(self):
        result = dest_file_ids_from_hits(
            file_hits=[
                {"fid": "episode-a", "cid": "dest-a"},
                {"fid": "episode-a", "cid": "dest-a"},
            ],
            folder_hits=[
                {"fid": "dest-a", "n": "Show A"},
                {"fid": "dest-a", "n": "Show A"},
            ],
            expected_ids=["episode-a"],
        )
        self.assertEqual(result, {"dest-a": ["episode-a"]})

    def test_grouping_conflicting_duplicate_folder_hit_returns_none(self):
        result = dest_file_ids_from_hits(
            file_hits=[{"fid": "episode-a", "cid": "dest-a"}],
            folder_hits=[
                {"fid": "dest-a", "cid": "library-a", "n": "Show A"},
                {"fid": "dest-a", "cid": "library-b", "n": "Show A"},
            ],
            expected_ids=["episode-a"],
        )
        self.assertIsNone(result)

    def test_grouping_ambiguous_file_destination_returns_none(self):
        result = dest_file_ids_from_hits(
            file_hits=[
                {"fid": "episode-a", "cid": "dest-a"},
                {"fid": "episode-a", "cid": "dest-b"},
            ],
            folder_hits=[
                {"fid": "dest-a", "n": "Show A"},
                {"fid": "dest-b", "n": "Show B"},
            ],
            expected_ids=["episode-a"],
        )
        self.assertIsNone(result)

    def test_movie_parent_is_dest(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-c-441531", "n": "C-拆弹专家-2017-[tmdb=441531]", "pid": "movie-parent"},
                {"cid": "recv-folder-402", "n": "拆弹专家 (2017) [tmdb=441531]", "pid": "redundant-cid"},
            ],
            expected_ids=["video-mkv-402"],
        )
        self.assertEqual(dest, "dest-c-441531")

    def test_season_parent_walks_up_to_show_root(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "dest-108978")

    def test_season_child_of_dest_walks_up_to_show_root(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "dest-108978")
        self.assertNotEqual(dest, "season-3")

    def test_file_parent_without_season_in_folder_hits_is_that_parent(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "season-3")

    def test_missing_file_is_incomplete(self):
        dest = dest_id_from_file_hits(
            file_hits=[],
            folder_hits=[],
            expected_ids=["video-mkv-402"],
        )
        self.assertEqual(dest, "incomplete")

    def test_located_subset_is_incomplete_until_all_expected_files_are_found(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-a", "cid": "dest-a", "n": "A.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-a", "n": "Show A", "pid": "tv-parent"},
            ],
            expected_ids=["ep-a", "ep-b"],
        )
        self.assertEqual(dest, "incomplete")

    def test_collect_file_ids_under_dest_includes_season_children(self):
        listed = {
            "dest-108978": [
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
            ],
            "season-3": [
                {"fid": "ep-s3-e1", "n": "Reacher.S03E01.mkv", "cid": "season-3"},
                {"fid": "ep-s3-e2", "n": "Reacher.S03E02.mkv", "cid": "season-3"},
            ],
        }

        def list_files(parent_id, limit=500):
            return list(listed.get(str(parent_id), []))

        self.assertEqual(
            collect_file_ids_under_dest("dest-108978", list_files),
            {"season-3", "ep-s3-e1", "ep-s3-e2"},
        )

    def test_two_library_roots_conflict(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-a", "cid": "dest-a", "n": "A.mkv"},
                {"fid": "ep-b", "cid": "dest-b", "n": "B.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-a", "n": "Show A", "pid": "tv-parent"},
                {"cid": "dest-b", "n": "Show B", "pid": "tv-parent"},
            ],
            expected_ids=["ep-a", "ep-b"],
        )
        self.assertEqual(dest, "conflict")


class IntakeIdentityCleanupTests(unittest.TestCase):
    def test_delete_empty_root_in_redundant(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="redundant-cid",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "delete",
        )

    def test_skip_when_root_is_dest(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="dest-c-441531",
                parent_id="movie-parent",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "skip",
        )

    def test_skip_when_root_already_gone(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid"},
            ),
            "skip",
        )

    def test_needs_action_when_root_sits_in_library(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="movie-parent",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "needs_action",
        )
