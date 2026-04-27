from __future__ import annotations

import pytest

from abridged_convert.cover_finder import (
    extract_content_ids,
    extract_episode_references,
    extract_title,
    find_best_cover,
)


REAL_WORLD_COVER_CASES = [
    (
        "Episode 3.5 - Champion (Alternate End).mkv",
        "Fate⧸Apocrabridged (old) Finale： ＂Champion＂ (Fate⧸Apocrypha Abridged) [VMuQDCNC69E].png",
        "title_similarity",
    ),
    (
        "Episode 6 - Malkuth Mayhem (ep 4-6) Compilation.mkv",
        "Fate⧸Apocrabridged Episodes 4, 5 and 6 (Fate⧸Apocrypha Abridged) [1N2SuEfsT0c].png",
        "episode_reference_overlap",
    ),
    (
        "She's my Trap Queen Dub.mkv",
        "She's my Trap Queen Dub-7ZEVH_4Ydeo.jpg",
        "exact_title",
    ),
    (
        "She's my trap queen.mkv",
        "She's my trap queen-A1YjRsr-oDs.jpg",
        "exact_title",
    ),
    (
        "NEVER Let Frieren Cook Again [GChq40Jlvpg].mkv",
        "We found out what he was cooking... [GChq40Jlvpg].png",
        "shared_id",
    ),
    (
        "Episode 2 - Nigasaki in Paris.mkv",
        "2.webp",
        "episode_number",
    ),
    (
        "Episode 0.2 - The Abusive Father (Benwain Debut).mkv",
        "0.2.jpg",
        "episode_number",
    ),
    (
        "Episode 0.5 - This is a College Class on Rap.mkv",
        "This is a College Class on Rap [2rV6g6BCqJc].webp",
        "exact_title",
    ),
    (
        "Episode 11.1 - Dababy's Last Chance.mkv",
        "Dababy's Last Chance [dW6IzGyQlrg].webp",
        "exact_title",
    ),
    (
        "Episode 13 - Tis the Reason to Hate This Season.mkv",
        "Tis the Reason to Hate This Season [GTEiINjJYls].png",
        "exact_title",
    ),
]

NO_MATCH_CASES = [
    "Episode 0.6 - Rapstar Revue.mkv",
    "Episode 14 - Kurosai Cinematic Universe.mkv",
]


class CoverFinderRegressionTests:
    def test_extract_title_strips_trailing_ids(self) -> None:
        assert extract_title("She's my Trap Queen Dub-7ZEVH_4Ydeo") == "she s my trap queen dub"
        assert extract_title("We found out what he was cooking... [GChq40Jlvpg]") == "we found out what he was cooking"

    def test_extract_content_ids_reads_bracketed_id(self) -> None:
        assert extract_content_ids("NEVER Let Frieren Cook Again [GChq40Jlvpg]") == {"GChq40Jlvpg"}

    def test_extract_episode_references_expands_ranges(self) -> None:
        assert extract_episode_references("Episode 6 - Malkuth Mayhem (ep 4-6) Compilation") == {"4", "5", "6"}

    @pytest.mark.parametrize(
        ("video_name", "expected_cover", "expected_strategy"),
        REAL_WORLD_COVER_CASES,
    )
    def test_real_world_regressions(
        self,
        tmp_path,
        video_name: str,
        expected_cover: str,
        expected_strategy: str,
    ) -> None:
        covers_dir = tmp_path / "covers"
        covers_dir.mkdir()
        (covers_dir / expected_cover).touch()
        (covers_dir / "unrelated bonus art.png").touch()

        match = find_best_cover(tmp_path / video_name, covers_dir)

        assert match is not None
        assert match.path.name == expected_cover
        assert match.strategy == expected_strategy

    @pytest.mark.parametrize("video_name", NO_MATCH_CASES)
    def test_returns_none_when_no_good_match_exists(self, tmp_path, video_name: str) -> None:
        covers_dir = tmp_path / "covers"
        covers_dir.mkdir()
        (covers_dir / "unrelated bonus art.png").touch()
        (covers_dir / "opening credits.jpg").touch()

        assert find_best_cover(tmp_path / video_name, covers_dir) is None
