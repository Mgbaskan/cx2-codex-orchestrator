from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure tests bootstrap and imports work
sys.path.insert(0, str(Path(__file__).parent.resolve()))
import _bootstrap  # type: ignore[import-untyped]

from cx2_cli import build_parser, main
from prompt_transport import (
    MAX_PROMPT_BYTES,
    PromptTransportError,
    ResolvedPromptSource,
    capture_multiline_paste,
    read_prompt_file,
    read_prompt_stdin,
    resolve_prompt_source,
    validate_prompt_bytes,
    validate_prompt_text,
)


class MockStdin:
    """Helper mock for stdin with a readable binary buffer."""
    def __init__(self, raw_bytes: bytes, is_tty: bool = False) -> None:
        self.buffer = io.BytesIO(raw_bytes)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def read(self) -> str:
        return self.buffer.getvalue().decode("utf-8", errors="surrogateescape")


class TestPromptTransport(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    # -------------------------------------------------------------
    # 1-16: Prompt File & Byte Ingestion Tests
    # -------------------------------------------------------------

    def test_01_utf8_file(self) -> None:
        p = self.temp_dir / "task.md"
        content = "Fix the login bug in auth service."
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_02_utf8_bom_file(self) -> None:
        p = self.temp_dir / "task_bom.md"
        content = "Fix the login bug with BOM header."
        p.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_03_turkish_unicode(self) -> None:
        p = self.temp_dir / "turkish.txt"
        content = "Giriş hatasını düzelt: İ, ı, ş, ğ, ü, ö, ç, Ğ, Ü, Ş, İ, Ö, Ç."
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_04_emoji(self) -> None:
        p = self.temp_dir / "emoji.txt"
        content = "Deploy to cluster 🚀 with sparkles ✨ and tests 🧪."
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_05_crlf_preserved(self) -> None:
        p = self.temp_dir / "crlf.txt"
        raw = b"Line 1\r\nLine 2\r\nLine 3\r\n"
        p.write_bytes(raw)
        res = read_prompt_file(p)
        self.assertEqual(res, "Line 1\r\nLine 2\r\nLine 3\r\n")

    def test_06_lf_preserved(self) -> None:
        p = self.temp_dir / "lf.txt"
        raw = b"Line 1\nLine 2\nLine 3\n"
        p.write_bytes(raw)
        res = read_prompt_file(p)
        self.assertEqual(res, "Line 1\nLine 2\nLine 3\n")

    def test_07_internal_blank_lines_preserved(self) -> None:
        p = self.temp_dir / "blanks.txt"
        content = "Header\n\n\nMiddle\n\nFooter"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_08_leading_indentation_preserved(self) -> None:
        p = self.temp_dir / "indents.py"
        content = "    def test():\n        return 42\n"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_09_trailing_newline_preserved(self) -> None:
        p = self.temp_dir / "trailing.txt"
        content = "Single line with newline\n"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_10_binary_nul_rejected(self) -> None:
        p = self.temp_dir / "binary.bin"
        p.write_bytes(b"Text before \x00 text after")
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("NUL bayt", str(ctx.exception))

    def test_11_invalid_utf8_rejected(self) -> None:
        p = self.temp_dir / "invalid.txt"
        p.write_bytes(b"Invalid UTF8 \xff\xfe random bytes")
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("UTF-8", str(ctx.exception))

    def test_12_empty_file_rejected(self) -> None:
        p = self.temp_dir / "empty.txt"
        p.write_bytes(b"")
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("boş", str(ctx.exception))

    def test_13_whitespace_only_rejected(self) -> None:
        p = self.temp_dir / "ws.txt"
        p.write_bytes(b"   \n\t  \r\n  ")
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("boş", str(ctx.exception))

    def test_14_missing_file_rejected(self) -> None:
        p = self.temp_dir / "nonexistent.txt"
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("bulunamadı", str(ctx.exception))

    def test_15_directory_rejected(self) -> None:
        p = self.temp_dir / "somedir"
        p.mkdir()
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("dosya değil", str(ctx.exception))

    def test_16_max_prompt_bytes_rejected(self) -> None:
        p = self.temp_dir / "oversized.txt"
        large_bytes = b"A" * (MAX_PROMPT_BYTES + 10)
        p.write_bytes(large_bytes)
        with self.assertRaises(PromptTransportError) as ctx:
            read_prompt_file(p)
        self.assertIn("izin verilen sınırı aşıyor", str(ctx.exception))

    # -------------------------------------------------------------
    # 17-20: Stdin Ingestion Tests
    # -------------------------------------------------------------

    def test_17_stdin_normal(self) -> None:
        content = "Piped prompt content from stdin."
        mock_stdin = MockStdin(content.encode("utf-8"), is_tty=False)
        with patch.object(sys, "stdin", mock_stdin):
            res = read_prompt_stdin(is_tty_error=True)
            self.assertEqual(res, content)

    def test_18_stdin_bom(self) -> None:
        content = "Piped prompt with BOM."
        raw = b"\xef\xbb\xbf" + content.encode("utf-8")
        mock_stdin = MockStdin(raw, is_tty=False)
        with patch.object(sys, "stdin", mock_stdin):
            res = read_prompt_stdin(is_tty_error=True)
            self.assertEqual(res, content)

    def test_19_stdin_invalid_utf8(self) -> None:
        raw = b"Invalid UTF8 in stdin: \xff\xfe"
        mock_stdin = MockStdin(raw, is_tty=False)
        with patch.object(sys, "stdin", mock_stdin):
            with self.assertRaises(PromptTransportError) as ctx:
                read_prompt_stdin(is_tty_error=True)
            self.assertIn("UTF-8", str(ctx.exception))

    def test_20_stdin_oversized(self) -> None:
        raw = b"B" * (MAX_PROMPT_BYTES + 50)
        mock_stdin = MockStdin(raw, is_tty=False)
        with patch.object(sys, "stdin", mock_stdin):
            with self.assertRaises(PromptTransportError) as ctx:
                read_prompt_stdin(is_tty_error=True)
            self.assertIn("izin verilen sınırı aşıyor", str(ctx.exception))

    def test_20b_stdin_tty_rejection(self) -> None:
        mock_stdin = MockStdin(b"some text", is_tty=True)
        with patch.object(sys, "stdin", mock_stdin):
            with self.assertRaises(PromptTransportError) as ctx:
                read_prompt_stdin(is_tty_error=True)
            self.assertIn("piped veya yönlendirilmiş girdi gerektirir", str(ctx.exception))

    # -------------------------------------------------------------
    # 21-28: Conflict & Equivalence Tests
    # -------------------------------------------------------------

    def test_21_positional_plus_prompt_file_conflict(self) -> None:
        parser = build_parser()
        p = self.temp_dir / "task.md"
        p.write_bytes(b"file content")
        args = parser.parse_args(["pos_prompt", "--prompt-file", str(p)])
        with self.assertRaises(PromptTransportError) as ctx:
            resolve_prompt_source(args, self.temp_dir)
        self.assertIn("Birden fazla prompt kaynağı", str(ctx.exception))

    def test_22_stdin_plus_prompt_file_conflict(self) -> None:
        parser = build_parser()
        p = self.temp_dir / "task.md"
        p.write_bytes(b"file content")
        args = parser.parse_args(["--stdin", "--prompt-file", str(p)])
        with self.assertRaises(PromptTransportError) as ctx:
            resolve_prompt_source(args, self.temp_dir)
        self.assertIn("Birden fazla prompt kaynağı", str(ctx.exception))

    def test_23_stdin_plus_positional_conflict(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pos_prompt", "--stdin"])
        with self.assertRaises(PromptTransportError) as ctx:
            resolve_prompt_source(args, self.temp_dir)
        self.assertIn("Birden fazla prompt kaynağı", str(ctx.exception))

    def test_24_attachment_plus_prompt_file_allowed(self) -> None:
        parser = build_parser()
        p = self.temp_dir / "task.md"
        p.write_bytes(b"task text")
        att = self.temp_dir / "spec.pdf"
        att.write_bytes(b"%PDF-1.4 dummy")
        args = parser.parse_args(["--prompt-file", str(p), "--file", str(att)])
        res = resolve_prompt_source(args, self.temp_dir)
        self.assertEqual(res.prompt, "task text")
        self.assertEqual(res.source_kind, "prompt-file")
        self.assertFalse(res.is_route_only)

    def test_25_route_file_loads_same_exact_text(self) -> None:
        parser = build_parser()
        p = self.temp_dir / "route_task.md"
        content = "Analyze this project architecture"
        p.write_bytes(content.encode("utf-8"))
        args = parser.parse_args(["--route-file", str(p)])
        res = resolve_prompt_source(args, self.temp_dir)
        self.assertEqual(res.prompt, content)
        self.assertEqual(res.source_kind, "route-file")
        self.assertTrue(res.is_route_only)

    def test_26_route_plus_route_file_conflict(self) -> None:
        parser = build_parser()
        p = self.temp_dir / "task.md"
        p.write_bytes(b"file content")
        args = parser.parse_args(["--route", "text prompt", "--route-file", str(p)])
        with self.assertRaises(PromptTransportError) as ctx:
            resolve_prompt_source(args, self.temp_dir)
        self.assertIn("Hem --route hem --route-file", str(ctx.exception))

    def test_27_route_file_plus_prompt_file_conflict(self) -> None:
        parser = build_parser()
        p = self.temp_dir / "task.md"
        p.write_bytes(b"file content")
        args = parser.parse_args(["--route-file", str(p), "--prompt-file", str(p)])
        with self.assertRaises(PromptTransportError) as ctx:
            resolve_prompt_source(args, self.temp_dir)
        self.assertIn("Route önizleme argümanları", str(ctx.exception))

    def test_28_logical_source_equivalence(self) -> None:
        content = "Refactor the authentication controller:\n  1. Add JWT\n  2. Test $env:HOME"
        # 1. From File
        p = self.temp_dir / "source_equiv.md"
        p.write_bytes(content.encode("utf-8"))
        res_file = read_prompt_file(p)

        # 2. From Stdin
        mock_stdin = MockStdin(content.encode("utf-8"), is_tty=False)
        with patch.object(sys, "stdin", mock_stdin):
            res_stdin = read_prompt_stdin(is_tty_error=True)

        # 3. From Paste
        paste_lines = content.splitlines() + [".send"]
        idx = 0

        def mock_input(prompt: str) -> str:
            nonlocal idx
            line = paste_lines[idx]
            idx += 1
            return line

        res_paste = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)

        self.assertEqual(res_file, content)
        self.assertEqual(res_stdin, content)
        self.assertEqual(res_paste, content)

    # -------------------------------------------------------------
    # 29-32: Large & Complex Structure Preservation Tests
    # -------------------------------------------------------------

    def test_29_500_line_prompt(self) -> None:
        lines = [f"Line {i:03d}: Proje modül analiz adımı (Türkçe: ğüşıöç)." for i in range(500)]
        content = "\n".join(lines)
        p = self.temp_dir / "500_lines.txt"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(len(res.splitlines()), 500)
        self.assertEqual(res, content)

    def test_30_64kb_prompt(self) -> None:
        chunk = "A" * 1024 + "\n"
        content = chunk * 64  # 64 KB + newlines
        p = self.temp_dir / "64kb.txt"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(len(res.encode("utf-8")), len(content.encode("utf-8")))
        self.assertEqual(res, content)

    def test_31_quotes_backticks_dollar_preserved(self) -> None:
        content = '`npm run test` with "$VAR" and \'single quotes\' and `backticks` and $env:PATH.'
        p = self.temp_dir / "symbols.txt"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    def test_32_json_yaml_code_fences_preserved(self) -> None:
        content = """```json
{
  "name": "cx2",
  "version": "2.0.8",
  "dependencies": {
    "react": "^19.0.0"
  }
}
```
```yaml
services:
  app:
    image: node:22
```"""
        p = self.temp_dir / "fences.md"
        p.write_bytes(content.encode("utf-8"))
        res = read_prompt_file(p)
        self.assertEqual(res, content)

    # -------------------------------------------------------------
    # 33-46: Interactive Paste Tests
    # -------------------------------------------------------------

    def test_33_paste_enters_capture_and_submits(self) -> None:
        inputs = ["Line 1", "Line 2", ".send"]
        idx = 0

        def mock_input(prompt: str) -> str:
            nonlocal idx
            val = inputs[idx]
            idx += 1
            return val

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        self.assertEqual(res, "Line 1\nLine 2")

    def test_34_paste_cancel_aborts(self) -> None:
        inputs = ["Line 1", "Line 2", ".cancel"]
        idx = 0

        def mock_input(prompt: str) -> str:
            nonlocal idx
            val = inputs[idx]
            idx += 1
            return val

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        self.assertIsNone(res)

    def test_35_paste_ctrl_c_aborts(self) -> None:
        def mock_input(prompt: str) -> str:
            raise KeyboardInterrupt()

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        self.assertIsNone(res)

    def test_36_paste_eof_aborts(self) -> None:
        def mock_input(prompt: str) -> str:
            raise EOFError()

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        self.assertIsNone(res)

    def test_37_paste_escaped_send_and_cancel(self) -> None:
        inputs = [
            "Normal line",
            "..send",
            "..cancel",
            "This contains .send inside text",
            ".send",
        ]
        idx = 0

        def mock_input(prompt: str) -> str:
            nonlocal idx
            val = inputs[idx]
            idx += 1
            return val

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        expected = "Normal line\n.send\n.cancel\nThis contains .send inside text"
        self.assertEqual(res, expected)

    def test_38_paste_blank_lines_and_turkish(self) -> None:
        inputs = [
            "Başlık: Türkçe test",
            "",
            "  - İkinci satır boşluklu",
            "",
            "Son satır: ğüşıöç ĞÜŞİÖÇ.",
            ".send",
        ]
        idx = 0

        def mock_input(prompt: str) -> str:
            nonlocal idx
            val = inputs[idx]
            idx += 1
            return val

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        expected = "Başlık: Türkçe test\n\n  - İkinci satır boşluklu\n\nSon satır: ğüşıöç ĞÜŞİÖÇ."
        self.assertEqual(res, expected)

    def test_39_paste_empty_buffer_returns_none(self) -> None:
        inputs = [".send"]
        idx = 0

        def mock_input(prompt: str) -> str:
            nonlocal idx
            val = inputs[idx]
            idx += 1
            return val

        res = capture_multiline_paste(input_func=mock_input, print_func=lambda *a: None)
        self.assertIsNone(res)

    def test_39b_paste_summary_reports_counts_without_content(self) -> None:
        inputs = iter(["Türkçe 🚀", "ikinci", ".send"])
        output: list[str] = []
        res = capture_multiline_paste(
            input_func=lambda _prompt: next(inputs),
            print_func=lambda value="": output.append(str(value)),
        )
        self.assertEqual(res, "Türkçe 🚀\nikinci")
        self.assertIn("2 satır", output[-1])
        self.assertIn(f"{len(res)} karakter", output[-1])
        self.assertNotIn("Türkçe", "\n".join(output))

    def test_39c_paste_exact_limit_and_over_limit(self) -> None:
        exact = "a" * MAX_PROMPT_BYTES
        output: list[str] = []
        accepted = capture_multiline_paste(
            input_func=lambda _prompt, values=iter([exact, ".send"]): next(values),
            print_func=lambda value="": output.append(str(value)),
        )
        self.assertEqual(accepted, exact)
        self.assertIn(f"{MAX_PROMPT_BYTES} karakter", output[-1])

        output = []
        rejected = capture_multiline_paste(
            input_func=lambda _prompt, values=iter([exact + "b", ".send"]): next(values),
            print_func=lambda value="": output.append(str(value)),
        )
        self.assertIsNone(rejected)
        self.assertIn("sınırı aşıyor", output[-1])

    def test_40_main_cli_prompt_file_integration(self) -> None:
        p = self.temp_dir / "exec_task.md"
        p.write_bytes(b"Refactor service layer")
        with patch("cx2_cli.execute_one_shot", return_value=0) as mock_exec:
            code = main(["--prompt-file", str(p)])
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()
            called_prompt = mock_exec.call_args[0][0]
            self.assertEqual(called_prompt, "Refactor service layer")

    def test_41_main_cli_route_file_integration(self) -> None:
        p = self.temp_dir / "route_task.md"
        p.write_bytes(b"Inspect security modules")
        with patch("cx2_cli.print_local_route") as mock_route:
            code = main(["--route-file", str(p)])
            self.assertEqual(code, 0)
            mock_route.assert_called_once()
            called_prompt = mock_route.call_args[0][0]
            self.assertEqual(called_prompt, "Inspect security modules")

    def test_42_main_cli_error_clean_exit(self) -> None:
        p = self.temp_dir / "nonexistent.md"
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            code = main(["--prompt-file", str(p)])
            self.assertEqual(code, 1)
            err_text = stderr_capture.getvalue()
            self.assertIn("[cx] prompt error:", err_text)
            self.assertIn("bulunamadı", err_text)


if __name__ == "__main__":
    unittest.main()
