from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from abridged_convert.cover_finder import extract_content_ids, extract_episode_references, extract_title, find_best_cover


class CoverFinderRegressionTests(unittest.TestCase):
    def test_extract_title_strips_trailing_ids(self) -> None:
        self.assertEqual(extract_title("She's my Trap Queen Dub-7ZEVH_4Ydeo"), "she s my trap queen dub")
        self.assertEqual(extract_title("We found out what he was cooking... [GChq40Jlvpg]"), "we found out what he was cooking")

    def test_extract_content_ids_reads_bracketed_id(self) -> None:
        self.assertEqual(extract_content_ids("NEVER Let Frieren Cook Again [GChq40Jlvpg]"), {"GChq40Jlvpg"})

    def test_extract_episode_references_expands_ranges(self) -> None:
        self.assertEqual(
            extract_episode_references("Episode 6 - Malkuth Mayhem (ep 4-6) Compilation"),
            {"4", "5", "6"},
        )

    def test_real_world_regressions(self) -> None:
        cases = [
            (
                "Series/[BlueDrakma] Fate Apocrabridged/Episode 3.5 - Champion (Alternate End).mkv",
                "Series/[BlueDrakma] Fate Apocrabridged/covers",
                "Fate⧸Apocrabridged (old) Finale： ＂Champion＂ (Fate⧸Apocrypha Abridged) [VMuQDCNC69E].png",
                "title_similarity",
            ),
            (
                "Series/[BlueDrakma] Fate Apocrabridged/Episode 6 - Malkuth Mayhem (ep 4-6) Compilation.mkv",
                "Series/[BlueDrakma] Fate Apocrabridged/covers",
                "Fate⧸Apocrabridged Episodes 4, 5 and 6 (Fate⧸Apocrypha Abridged) [1N2SuEfsT0c].png",
                "episode_reference_overlap",
            ),
            (
                "Series/[Kurosai] Kurosai Shorts/She's my Trap Queen Dub.mkv",
                "Series/[Kurosai] Kurosai Shorts/covers",
                "She's my Trap Queen Dub-7ZEVH_4Ydeo.jpg",
                "exact_title",
            ),
            (
                "Series/[Kurosai] Kurosai Shorts/She's my trap queen.mkv",
                "Series/[Kurosai] Kurosai Shorts/covers",
                "She's my trap queen-A1YjRsr-oDs.jpg",
                "exact_title",
            ),
            (
                "Series/[Kurosai] Kurosai Shorts/NEVER Let Frieren Cook Again [GChq40Jlvpg].mkv",
                "Series/[Kurosai] Kurosai Shorts/covers",
                "We found out what he was cooking... [GChq40Jlvpg].png",
                "shared_id",
            ),
            (
                "Series/[Kurosai] Boundary Bridge/Episode 2 - Nigasaki in Paris.mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
                "2.webp",
                "episode_number",
            ),
            (
                "Series/[Kurosai] Boundary Bridge/Episode 0.2 - The Abusive Father (Benwain Debut).mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
                "0.2.jpg",
                "episode_number",
            ),
            (
                "Series/[Kurosai] Boundary Bridge/Episode 0.5 - This is a College Class on Rap.mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
                "This is a College Class on Rap [2rV6g6BCqJc].webp",
                "exact_title",
            ),
            (
                "Series/[Kurosai] Boundary Bridge/Episode 11.1 - Dababy's Last Chance.mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
                "Dababy's Last Chance [dW6IzGyQlrg].webp",
                "exact_title",
            ),
            (
                "Series/[Kurosai] Boundary Bridge/Episode 13 - Tis the Reason to Hate This Season.mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
                "Tis the Reason to Hate This Season [GTEiINjJYls].png",
                "exact_title",
            ),
        ]

        for video_rel, covers_rel, expected_cover, expected_strategy in cases:
            with self.subTest(video=video_rel):
                match = find_best_cover(
                    WORKSPACE_ROOT / video_rel,
                    WORKSPACE_ROOT / covers_rel,
                )
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.path.name, expected_cover)
                self.assertEqual(match.strategy, expected_strategy)

    def test_returns_none_when_no_good_match_exists(self) -> None:
        cases = [
            (
                "Series/[Kurosai] Boundary Bridge/Episode 0.6 - Rapstar Revue.mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
            ),
            (
                "Series/[Kurosai] Boundary Bridge/Episode 14 - Kurosai Cinematic Universe.mkv",
                "Series/[Kurosai] Boundary Bridge/covers",
            ),
        ]

        for video_rel, covers_rel in cases:
            with self.subTest(video=video_rel):
                self.assertIsNone(
                    find_best_cover(
                        WORKSPACE_ROOT / video_rel,
                        WORKSPACE_ROOT / covers_rel,
                    )
                )


if __name__ == "__main__":
    unittest.main()
