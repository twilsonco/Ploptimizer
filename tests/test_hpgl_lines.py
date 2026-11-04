"""Unit tests for the cosmetic HPGL line-breaking formatter.

Pins :func:`plt_optimizer.core.hpgl_lines.format_hpgl_lines`: the
EngraveLab-style layout (one command per line, bare ``PD;``/``PU;`` glued to
the following arc) and, critically, the parse-identity guarantee -- inserting
newlines must never change the parsed document.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plt_optimizer.core.hpgl_lines import format_hpgl_lines
from plt_optimizer.core.parser import PLTParser

_TESTS_DEPS = Path(__file__).parent.parent / "tests_deps"


class TestBasicLayout:
    """Every command lands on its own line."""

    def test_empty_content_yields_empty_string(self) -> None:
        assert format_hpgl_lines("") == ""

    def test_command_free_content_yields_empty_string(self) -> None:
        """Whitespace/junk with no ``;``-terminated command emits nothing."""
        assert format_hpgl_lines("   \n  \r\n") == ""

    def test_single_command(self) -> None:
        assert format_hpgl_lines("IN;") == "IN;\n"

    def test_header_footer_one_per_line(self) -> None:
        assert format_hpgl_lines("IN;PA;SP;") == "IN;\nPA;\nSP;\n"

    def test_tool_option_headers_are_not_merged(self) -> None:
        """``VS``/``ZO``/``VZ``/``ZU`` each get their own line (reference parity)."""
        content = "IN;VS0.80;ZO123,1;VZ2.00;ZO102,0;ZU200;PA;"
        assert format_hpgl_lines(content) == (
            "IN;\nVS0.80;\nZO123,1;\nVZ2.00;\nZO102,0;\nZU200;\nPA;\n"
        )

    def test_exact_compact_document(self) -> None:
        content = "IN;PA;PU0,0;PD10,0;PU5,5;PD15,5;SP;"
        assert format_hpgl_lines(content) == ("IN;\nPA;\nPU0,0;\nPD10,0;\nPU5,5;\nPD15,5;\nSP;\n")

    def test_existing_newlines_are_normalized(self) -> None:
        """Already-reflowed content is returned unchanged (idempotent)."""
        content = "IN;\nPA;\nPU0,0;\nPD10,0;\nSP;\n"
        assert format_hpgl_lines(content) == content

    def test_crlf_input_is_normalized(self) -> None:
        content = "IN;\r\nPA;\r\nPU0,0;\r\nSP;\r\n"
        assert format_hpgl_lines(content) == "IN;\nPA;\nPU0,0;\nSP;\n"

    def test_inter_command_whitespace_is_dropped(self) -> None:
        content = "IN;   PA;\n\t PU0,0; SP;"
        assert format_hpgl_lines(content) == "IN;\nPA;\nPU0,0;\nSP;\n"

    def test_single_trailing_newline(self) -> None:
        result = format_hpgl_lines("IN;PA;SP;")
        assert result.endswith("SP;\n")
        assert not result.endswith("\n\n")


class TestBarePenCommandGlue:
    """A bare ``PD;``/``PU;`` stays on the same line as the arc it governs."""

    def test_bare_pd_glues_to_aa(self) -> None:
        content = "PU100,100;PD;AA50,100,-90;"
        assert format_hpgl_lines(content) == "PU100,100;\nPD;AA50,100,-90;\n"

    def test_bare_pu_glues_to_aa(self) -> None:
        content = "PU100,100;PU;AA50,100,-90;"
        assert format_hpgl_lines(content) == "PU100,100;\nPU;AA50,100,-90;\n"

    def test_bare_pd_glues_to_ar_and_ci(self) -> None:
        assert format_hpgl_lines("PD;AR50,100,25;") == "PD;AR50,100,25;\n"
        assert format_hpgl_lines("PD;CI25;") == "PD;CI25;\n"

    def test_bare_pd_chain_stays_one_per_line(self) -> None:
        """The four-quarter-circle drill hole: every arc on its own line."""
        content = "PU254,1080;PD254,1080;PD;AA254,1016,90;PD;AA254,1016,90;"
        assert format_hpgl_lines(content) == (
            "PU254,1080;\nPD254,1080;\nPD;AA254,1016,90;\nPD;AA254,1016,90;\n"
        )

    def test_bare_pd_at_end_of_content_is_left_alone(self) -> None:
        """No following arc: the bare command simply gets its own line."""
        assert format_hpgl_lines("PU0,0;PD;") == "PU0,0;\nPD;\n"

    def test_bare_pd_followed_by_non_arc_is_left_alone(self) -> None:
        assert format_hpgl_lines("PD;SP;") == "PD;\nSP;\n"
        assert format_hpgl_lines("PD;PU10,20;") == "PD;\nPU10,20;\n"

    def test_pd_with_coordinates_is_not_glued(self) -> None:
        """Only a *bare* pen command glues; ``PDx,y`` keeps its own line."""
        assert format_hpgl_lines("PD10,20;AA50,100,90;") == "PD10,20;\nAA50,100,90;\n"


class TestBatchedPolylineKeptVerbatim:
    """A multi-pair ``PD``/``PU`` stays ONE command on ONE line.

    Splitting a batched polyline into per-pair commands is *not* cosmetic:
    the parser takes a batched ``PD``'s first pair as the path's
    ``pen_up_position``, and a detached ``PD`` changes how a following
    ``AA`` attaches. The formatter therefore only inserts newlines.
    """

    def test_batched_pd_stays_on_one_line(self) -> None:
        content = "PU0,0;PD3000,0,3000,1000,0,1000,0,0;"
        assert format_hpgl_lines(content) == "PU0,0;\nPD3000,0,3000,1000,0,1000,0,0;\n"

    def test_batched_pu_stays_on_one_line(self) -> None:
        assert format_hpgl_lines("PU0,0,10,20,30,40;") == "PU0,0,10,20,30,40;\n"

    def test_arc_is_never_split(self) -> None:
        """``AA`` carries (cx, cy, sweep) -- three fields, not coordinate pairs."""
        assert format_hpgl_lines("PD;AA254.000,1016.000,90.000;") == (
            "PD;AA254.000,1016.000,90.000;\n"
        )

    def test_header_is_never_split(self) -> None:
        assert format_hpgl_lines("ZO124,50;") == "ZO124,50;\n"
        assert format_hpgl_lines("SP1;") == "SP1;\n"

    def test_odd_length_parameter_list_is_untouched(self) -> None:
        assert format_hpgl_lines("PD100,200,300;") == "PD100,200,300;\n"

    def test_single_pair_is_left_verbatim(self) -> None:
        assert format_hpgl_lines("PD100,200;") == "PD100,200;\n"

    def test_batched_pd_is_parse_identical(self) -> None:
        """The batched command survives reflow with its path structure intact."""
        parser = PLTParser()
        content = "IN;PA;PU0,0;PD1000,0,1000,1000,0,1000,0,0;SP;"
        compact = parser.parse_string(content)
        formatted = parser.parse_string(format_hpgl_lines(content))

        assert len(formatted.stroke_paths) == len(compact.stroke_paths) == 1
        assert formatted.total_segments == compact.total_segments == 4
        assert formatted.cutting_distance() == pytest.approx(compact.cutting_distance())
        assert formatted.stroke_paths[0].pen_up_position == compact.stroke_paths[0].pen_up_position


class TestIdempotence:
    """Reflowing already-reflowed content is a no-op."""

    @pytest.mark.parametrize(
        "content",
        [
            "IN;PA;SP;",
            "IN;PA;PU0,0;PD10,0,20,0;SP;",
            "PU100,100;PD;AA50,100,-90;PD;AA50,100,-90;",
            "IN;VS0.80;ZO123,1;PA;PU0,0;PD1,1;SP;",
        ],
    )
    def test_format_is_idempotent(self, content: str) -> None:
        once = format_hpgl_lines(content)
        assert format_hpgl_lines(once) == once


class TestParseIdentity:
    """The transform is cosmetic: the parsed document is unchanged."""

    @staticmethod
    def _fingerprint(doc: object) -> tuple[int, int, int, int, float]:
        """Structural signature: header/path/footer counts + segments + distance."""
        return (
            len(doc.header_commands),  # type: ignore[attr-defined]
            len(doc.stroke_paths),  # type: ignore[attr-defined]
            len(doc.footer_commands),  # type: ignore[attr-defined]
            doc.total_segments,  # type: ignore[attr-defined]
            doc.cutting_distance(),  # type: ignore[attr-defined]
        )

    @pytest.mark.parametrize(
        "content",
        [
            "IN;PA;PU0,0;PD10,0,20,0,20,10,0,10,0,0;SP;",
            "IN;PA;PU100,100;PD;AA50,100,-90;PD;AA50,100,-90;SP;",
            "IN;VS0.80;PA;SP1;PU0,0;PD5,5;SP2;PU10,10;PD20,20;SP;",
        ],
    )
    def test_synthetic_content_round_trips(self, content: str) -> None:
        parser = PLTParser()
        assert self._fingerprint(parser.parse_string(format_hpgl_lines(content))) == (
            self._fingerprint(parser.parse_string(content))
        )

    def test_bare_pd_arc_path_split_survives(self) -> None:
        """The glued ``PD;AA`` line keeps the parser's UP->DOWN path split.

        A bare ``PD;`` that lowers the pen opens a fresh ``StrokePath`` so the
        rapid travel into the position survives as ``pen_up_position``. The
        glue must not collapse that transition.
        """
        parser = PLTParser()
        content = "PU0,0;PD10,0;PU20,0;PD;AA25,5,90;"
        compact = parser.parse_string(content)
        formatted = parser.parse_string(format_hpgl_lines(content))

        assert len(formatted.stroke_paths) == len(compact.stroke_paths) == 2
        assert formatted.rapid_distance() == pytest.approx(compact.rapid_distance())
        assert formatted.stroke_paths[-1].pen_up_position is not None
        assert formatted.stroke_paths[-1].pen_up_position.x == 20
        assert formatted.stroke_paths[-1].pen_up_position.y == 0

    def test_batched_pd_stays_one_path(self) -> None:
        """A batched ``PD`` is kept verbatim: one path, same segment count."""
        parser = PLTParser()
        content = "IN;PA;PU0,0;PD1000,0,1000,1000,0,1000,0,0;SP;"
        compact = parser.parse_string(content)
        formatted = parser.parse_string(format_hpgl_lines(content))

        assert len(formatted.stroke_paths) == len(compact.stroke_paths) == 1
        assert formatted.total_segments == compact.total_segments == 4
        assert formatted.cutting_distance() == pytest.approx(compact.cutting_distance())
        assert formatted.rapid_distance() == pytest.approx(compact.rapid_distance())

    def test_all_reference_fixtures_are_parse_identical(self) -> None:
        """Every frozen fixture reflows without changing its parsed geometry."""
        parser = PLTParser()
        fixtures = sorted(_TESTS_DEPS.glob("*.plt"))
        assert fixtures, f"no fixtures found under {_TESTS_DEPS}"

        for fixture in fixtures:
            content = fixture.read_text(encoding="utf-8")
            compact = parser.parse_string(content)
            formatted = parser.parse_string(format_hpgl_lines(content))
            assert self._fingerprint(formatted) == self._fingerprint(compact), fixture.name
            assert formatted.rapid_distance() == pytest.approx(compact.rapid_distance()), (
                fixture.name
            )

    def test_reference_fixtures_are_already_one_command_per_line(self) -> None:
        """The EngraveLab fixtures pin the layout this formatter produces.

        Reflowing a reference file changes nothing but its line terminators,
        which is the definition of "we mimic their behavior".
        """
        for fixture in sorted(_TESTS_DEPS.glob("*.plt")):
            content = fixture.read_text(encoding="utf-8")
            formatted = format_hpgl_lines(content)
            assert formatted.count("\n") == content.replace("\r\n", "\n").count("\n"), fixture.name
