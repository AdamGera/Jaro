import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dictation_output import (inspect_output_script, keeps_rewrite_numbers, merge_vocabulary, numbers_only, parse_profiles, process, profile_for, review_output_script,
                              remove_trailing_punctuation, scoped_replacements)


class DictationOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_then_app_then_default(self):
        rules = parse_profiles("default | compact\napp:Notes | replace, trim\nsite:*.example.com | format")
        self.assertEqual(profile_for(rules, "Notes", "com.apple.Notes", "m.example.com").steps, ("format",))
        self.assertEqual(profile_for(rules, "Notes", "com.apple.Notes", "example.com.attacker.invalid").steps,
                         ("replace", "trim"))
        self.assertEqual(profile_for(rules, "Other").steps, ("compact",))
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_profiles("default | compact\nsite: | something")

    async def test_structured_scope_and_vocabulary_order(self):
        profiles = parse_profiles(json.dumps([
            {"target": "default", "steps": ["compact"], "instruction": "Default instructions"},
            {"target": "app:com.apple.Notes", "steps": [], "vocabulary": ["Jard", "Mira", "mira"]},
            {"target": "site:*.example.com", "steps": ["rewrite"], "instruction": "Use short paragraphs."},
            {"target": "site:*.example.com", "steps": ["sentence"]},
        ]))
        app = profile_for(profiles, "Notes", "com.apple.notes")
        self.assertEqual(app.steps, ())
        self.assertEqual(merge_vocabulary(app.vocabulary, ["JARD", "Shared", *(str(i) for i in range(32))]),
                         ["Jard", "Mira", "Shared", *(str(i) for i in range(29))])
        page = profile_for(profiles, "Notes", "com.apple.Notes", "www.example.com")
        self.assertEqual(page.steps, ("rewrite",))
        self.assertEqual(page.instruction, "Use short paragraphs.")
        self.assertEqual(profile_for(profiles, "Notes", "com.apple.Notes", "example.com"), app)
        with self.assertRaisesRegex(ValueError, "32 vocabulary terms"):
            parse_profiles(json.dumps([{"target": "default", "vocabulary": ["word"] * 33}]))

    def test_website_selectors_match_canonical_hosts_without_broadening_wildcards(self):
        for selector, page in (
            ("https://EXAMPLE.com/settings", "https://example.com/work"),
            ("example.com:3000", "example.com."),
            ("bücher.example", "https://xn--bcher-kva.example/"),
            ("xn--fa-hia.example", "https://faß.example/"),
            ("https://[::1]:8765/work", "::1"),
        ):
            profiles = parse_profiles(f"site:{selector} | lowercase\nsite:{page} | trim\napp:Notes | compact")
            self.assertIs(profile_for(profiles, "Notes", host=page), profiles[0])
        profiles = parse_profiles("site:*.bücher.example. | lowercase\napp:Notes | compact")
        self.assertIs(profile_for(profiles, "Notes", host="https://sub.xn--bcher-kva.example:9000"), profiles[0])
        for nonmatch in ("bücher.example", "xn--bcher-kva.example.invalid", "badbücher.example",
                         "https://sub.bücher.example@elsewhere.invalid", "https://sub.bücher.example:bad"):
            self.assertIs(profile_for(profiles, "Notes", host=nonmatch), profiles[1])

    def test_application_selectors_accept_finder_name_and_optional_app_suffix(self):
        profiles = parse_profiles("app:Visual Studio Code.APP | trim\napp:com.microsoft.VSCode | lowercase\ndefault | compact")
        self.assertIs(profile_for(profiles, "Code", "com.microsoft.VSCode",
                                  bundle_name="Visual Studio Code"), profiles[0])
        self.assertIs(profile_for(profiles, "Code", "COM.MICROSOFT.VSCODE"), profiles[1])
        self.assertIs(profile_for(profiles, "Visual Studio"), profiles[2])

    async def test_native_steps_work_in_preview_and_delivery_in_order(self):
        steps = parse_profiles("default | lowercase, spongebob-case, no-trailing-punctuation")[0].steps
        for preview in (True, False):
            result = await process("Sponge 12 Bob.", steps, preview=preview)
            self.assertEqual(result.text, "sPoNgE 12 bOb")
            self.assertFalse(result.warnings)
        self.assertEqual((await process("ABCD", ("spongebob-case", "lowercase"))).text, "abcd")
        self.assertEqual((await process("Æß-É", ("spongebob-case",))).text, "æSS-é")
        self.assertEqual((await process("Good, dog?!…", ("lowercase", "script"))).text, "Good, dog?!…")

    async def test_processing_observes_executed_stages_and_fallbacks(self):
        async def rewrite(text):
            await asyncio.sleep(.015)
            return text.replace("agenda", "schedule")

        result = await process("The agenda is ready.", ("rewrite", "lowercase"), rewrite=rewrite)
        self.assertEqual(result.text, "the schedule is ready.")
        self.assertTrue(result.processing.rewrite_attempted)
        self.assertGreaterEqual(result.processing.latency_ms, 10)
        self.assertEqual(result.processing.fallbacks, ())

        async def fail(_):
            raise ValueError("Failed in a private execution context")

        result = await process("AGENDA", ("rewrite", "lowercase", "script", "sentence"), rewrite=fail)
        self.assertEqual(result.text, "AGENDA")
        self.assertTrue(result.processing.rewrite_attempted)
        self.assertEqual(result.processing.fallbacks, ("rewrite", "transformations"))

        for value, steps, options in (
            ("1200", ("rewrite",), {}),
            ("agenda", ("replace",), {}),
            ("agenda", ("script", "rewrite"), {"preview": True, "builtins_only": True}),
        ):
            self.assertIsNone((await process(value, steps, rewrite=rewrite, **options)).processing)

    def test_trailing_punctuation_keeps_quotes_spaces_and_non_sentence_symbols(self):
        for text, expected in (
            ("Good dog. \t", "Good dog \t"), ("好的。", "好的"), ("你好；：", "你好"),
            ("Wait, what?!… \t", "Wait, what?!… \t"), ("Are you coming?", "Are you coming?"),
            ("Ship it!", "Ship it!"), ("你好？", "你好？"), ("太好了！", "太好了！"),
            ("Really?.", "Really?"), ("Well...", "Well..."), ("...", "..."),
            ('He said "hi."', 'He said "hi."'), ("(see above)", "(see above)"), ("up 5%", "up 5%"),
            ("   ", "   "), ("", ""),
        ):
            self.assertEqual(remove_trailing_punctuation(text), expected)

    async def test_steps_run_in_listed_order(self):
        text = "um please say line breaks"
        corrected = await process(text, ("fillers", "replace", "format"), "line breaks => new line second")
        self.assertEqual(corrected.text, "please say\nsecond")
        self.assertFalse(corrected.warnings)
        self.assertEqual((await process(text, ("format", "replace", "fillers"), "line breaks => new line second")).text,
                         "please say new line second")
        self.assertEqual((await process("jard at sign example", ("symbols",))).text, "jard@example")

    async def test_profile_corrections_follow_shared_rules_and_run_without_a_step(self):
        rules = parse_profiles(json.dumps([
            {"target": "app:Notes", "corrections": "documents => drafts"},
            {"target": "site:example.com", "corrections": "documents => reports"},
        ]))
        profile = profile_for(rules, "Notes", host="example.com")
        result = await process("Send notes.", profile.steps, "notes => documents", corrections=profile.corrections)
        self.assertEqual(result.text, "Send reports.")
        profile = profile_for(rules, "Notes", host="other.example.com")
        result = await process("Send notes.", profile.steps, "notes => documents", corrections=profile.corrections)
        self.assertEqual(result.text, "Send drafts.")
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_profiles(json.dumps([{"target": "default", "corrections": "notes => files\nmissing delimiter"}]))

    async def test_scoped_phrases_prefer_longest_and_do_not_cascade_within_a_pass(self):
        rules = "open => closed\nopen code => OpenCode\ncode then open => combined\nclosed => shut"
        self.assertEqual(scoped_replacements("open code then open", rules), "closed combined")
        self.assertEqual(scoped_replacements("OPEN CODE, reopen code.", rules), "OpenCode, reopen code.")
        self.assertEqual(scoped_replacements("a b c", "b c => BC\na b => AB"), "AB c")
        self.assertEqual(scoped_replacements("这个issue很重要 and issue_id", "issue => Issue"),
                         "这个Issue很重要 and issue_id")
        self.assertEqual(scoped_replacements("Breathe the the air.", "the the => the"), "Breathe the air.")
        self.assertEqual(scoped_replacements("I, um, uh, think so.", "um =>\nuh =>"), "I, think so.")
        self.assertEqual(scoped_replacements("Um, the build passed.", "um =>"), "The build passed.")

    async def test_explicit_replacements_keep_their_place_in_the_pipeline(self):
        result = await process("note line break task", ("format", "replace"), "note => item",
                               corrections="item => entry\nline break => new line")
        self.assertEqual(result.text, "entry new line task")

    async def test_numbers_only_bypass_rewrite_after_corrections(self):
        rewritten = []

        async def rewrite(text):
            rewritten.append(text)
            return text

        for text in ("1,355", "Twenty-one point five", "negative twenty-one", "one hundred and forty two", "١٢٫٥"):
            self.assertTrue(numbers_only(text), text)
            self.assertEqual((await process(text, ("rewrite",), rewrite=rewrite)).text, text)
        for text in ("", "And.", "The dataset has 1355 evaluation windows.", "Call me at five."):
            self.assertFalse(numbers_only(text), text)
        result = await process("test counts", ("rewrite", "sentence"), rewrite=rewrite,
                               corrections="test counts => one, two")
        self.assertEqual(result.text, "One, two.")
        self.assertEqual(rewritten, [])
        await process("Call me at five.", ("rewrite",), rewrite=rewrite)
        self.assertEqual(rewritten, ["Call me at five."])

    def test_rewrite_preserves_numeric_spelling(self):
        source = "Keep 1200 labels, loss −12.5%, range 8–14, batch 004, meeting 08:00, and subtotal ١٢٫٥."
        self.assertTrue(keeps_rewrite_numbers(source, source.replace("Keep", "Retain")))
        for original, changed in (
            ("1200", "1,200"), ("1200", "1 200"), ("−12.5%", "12.5%"), ("−12.5%", "−12:5%"),
            ("−12.5%", "−12.5"), ("8–14", "8-14"), ("004", "4"), ("08:00", "8:00"), ("١٢٫٥", "١٣٫٥"),
        ):
            with self.subTest(original=original, changed=changed):
                self.assertFalse(keeps_rewrite_numbers(source, source.replace(original, changed)))
        self.assertFalse(keeps_rewrite_numbers("Keep 42, then 12.", "Keep 12, then 42."))
        self.assertFalse(keeps_rewrite_numbers("We counted 1200.", "We counted one thousand two hundred."))

    def test_rewrite_allows_only_supported_list_markers(self):
        numbered = "1. Order 1200 labels.\n2. Record −12.5%."
        for cues, expected in (
            ("Point one, order 1200 labels. Point two, record −12.5%.", True),
            ("Number 1, order 1200 labels. Number 2, record −12.5%.", True),
            ("First, order 1200 labels, second, record −12.5%.", True),
            ("First order 1200 labels. Then record −12.5%.", False),
        ):
            with self.subTest(source=cues):
                self.assertEqual(keeps_rewrite_numbers(cues, numbered), expected)
        self.assertTrue(keeps_rewrite_numbers("Point twenty-one, call Sam. Point twenty-two, call Priya.",
                                             "21) Call Sam.\n22) Call Priya."))
        self.assertTrue(keeps_rewrite_numbers("Point 1, call Sam. Point 2, call Priya.",
                                             "- Call Sam.\n- Call Priya."))
        self.assertTrue(keeps_rewrite_numbers("1. Order 1200 labels.\n2. Record −12.5%.", numbered))
        self.assertFalse(keeps_rewrite_numbers("First, buy 1 label. Second, buy 2 ribbons.",
                                              "1. Buy a label.\n2. Buy ribbons."))
        self.assertFalse(keeps_rewrite_numbers("Readings were zero point one and zero point two.",
                                              "1. First reading.\n2. Second reading."))
        self.assertFalse(keeps_rewrite_numbers("Point one, call. Point two, write.", "1. Call.\n3. Write."))
        self.assertFalse(keeps_rewrite_numbers("Point one, use 1200. Point two, use −12.5%.",
                                              "1. Use 1,200.\n2. Use −12.5%."))

    async def test_local_script_is_opt_in_and_skipped_in_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "fixture"
            tool.write_text("#!/bin/sh\n/usr/bin/tr '[:lower:]' '[:upper:]'\n")
            tool.chmod(0o700)
            unapproved = await process("make me loud", ("script",), script=str(tool))
            self.assertEqual(unapproved.text, "make me loud")
            self.assertIn("Approve the selected script", unapproved.warnings[0])
            digest = inspect_output_script(tool)
            self.assertEqual((await process("make me loud", ("script",), script=str(tool), script_digest=digest)).text,
                             "MAKE ME LOUD")
            self.assertEqual((await process("make me loud", ("script",), script=str(tool), preview=True)).text,
                             "make me loud")
            failed = await process("hello", ("sentence", "script"))
            self.assertEqual(failed.text, "hello")
            self.assertIn("executable local script", failed.warnings[0])

    async def test_script_approval_rejects_changed_contents_and_revoked_queued_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "fixture"
            tool.write_text('#!/bin/sh\nprintf "original:%s:" "$JARO_OUTPUT_SCRIPT"\n/bin/cat\n')
            tool.chmod(0o700)
            digest = inspect_output_script(tool)
            opts = {"script": str(tool), "script_digest": digest}
            self.assertEqual((await process("hello", ("script",), **opts)).text, f"original:{tool}:hello")
            tool.write_text("#!/bin/sh\nprintf changed\n")
            changed = await process("hello", ("sentence", "script"), **opts)
            self.assertEqual(changed.text, "hello")
            self.assertIn("script changed", changed.warnings[0])
            opts["script_digest"] = inspect_output_script(tool)
            allows = iter((True, False))
            revoked = await process("hello", ("script",), **opts, script_allowed=lambda: next(allows))
            self.assertEqual(revoked.text, "hello")
            self.assertIn("Approval for the output script changed", revoked.warnings[0])
            self.assertEqual((await process("hello", ("script",), **opts)).text, "changed")

    async def test_named_catalog_runs_ordered_steps_from_the_approved_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            tool = root / "transform"
            tool.write_text('#!/bin/sh\nprintf "%s:" "$JARO_TRANSFORM_ID"\n/bin/cat suffix\nprintf ":"\n/bin/cat\n')
            tool.chmod(0o700)
            suffix = root / "suffix"
            suffix.write_text("APPROVED")
            path = root / "output.json"
            path.write_text(json.dumps({"transformations": {
                "prefix": {"name": "Add prefix", "entry": "transform", "files": ["suffix"]},
                "label": {"name": "Add label", "entry": "transform", "files": ["suffix"], "timeout": 2000},
            }}))
            reviewed = review_output_script(path)
            self.assertEqual([row["name"] for row in reviewed["transforms"]], ["Add prefix", "Add label"])
            profile = parse_profiles("default | local:prefix, lowercase, local:label")[0]
            options = {"script": str(path), "script_digest": reviewed["digest"]}
            result = await process("WORDS", profile.steps, **options)
            self.assertEqual(result.text, "label:APPROVED:prefix:approved:words")
            self.assertEqual(result.warnings, ())
            preview = await process("WORDS", profile.steps, builtins_only=True)
            self.assertEqual(preview.text, "words")
            self.assertEqual(preview.warnings, ())
            suffix.write_text("CHANGED")
            changed = await process("WORDS", profile.steps, **options)
            self.assertEqual(changed.text, "WORDS")
            self.assertIn("catalog changed", changed.warnings[0])
            self.assertNotEqual(review_output_script(path)["digest"], reviewed["digest"])

    async def test_catalog_does_not_follow_escaping_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            tool = root / "transform"
            tool.write_text("#!/bin/sh\n/bin/cat\n")
            tool.chmod(0o700)
            path = root / "output.json"
            for file, message in (("../outside", "inside the catalog folder"), ("outside", "missing or unavailable")):
                if file == "outside":
                    (root / file).symlink_to("/usr/bin/yes")
                path.write_text(json.dumps({"transformations": {"prefix": {"entry": "transform", "files": [file]}}}))
                with self.assertRaisesRegex(ValueError, message):
                    review_output_script(path)

    async def test_failed_rewrite_keeps_previous_stage_and_continues(self):
        async def rewrite(text):
            self.assertEqual(text, "corrected words")
            raise ValueError("The rewrite failed.")

        result = await process("spoken words", ("replace", "rewrite", "sentence"), "spoken => corrected", rewrite=rewrite)
        self.assertEqual(result.text, "Corrected words.")
        self.assertEqual(result.warnings, ("The rewrite failed.",))

    async def test_builtins_only_runs_corrections_and_skips_rewrites_and_scripts(self):
        async def rewrite(_):
            self.fail("A sample preview must not run a rewrite.")

        result = await process("JARO AUDIO.", ("lowercase", "rewrite", "script", "no-trailing-punctuation"),
                               corrections="Jaro Audio => Jaro", rewrite=rewrite, builtins_only=True)
        self.assertEqual(result.text, "jaro")
        self.assertEqual(result.warnings, ())
        kept = await process("JARO AUDIO?!", ("lowercase", "rewrite", "script", "no-trailing-punctuation"),
                             corrections="Jaro Audio => Jaro", rewrite=rewrite, builtins_only=True)
        self.assertEqual(kept.text, "jaro?!")

    async def test_alternate_spellings_match_whole_phrases_before_output_steps(self):
        result = await process("MAY LEAN, may, maybe, and MAY LEAN.", ("replace", "lowercase"),
                               replacements="Mei Lin => Mei Lin (team)",
                               aliases=[["may", "Mai"], ["may lean", "Mei Lin"], ["lin", "Lynn"]])
        self.assertEqual(result.text, "mei lin (team), mai, maybe, and mei lin (team).")
        self.assertEqual(result.warnings, ())
        invalid = await process("keep these words", ("sentence",), aliases=[["invalid", ""]])
        self.assertEqual(invalid.text, "Keep these words.")
        self.assertEqual(invalid.warnings, ("The saved alternate spellings are invalid.",))

    async def test_script_deadline_kills_a_program_that_never_reads_its_input(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "wait"
            tool.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
            tool.chmod(0o700)
            text = "a" * 65_536
            result = await asyncio.wait_for(process(text, ("script",), script=str(tool),
                                                    script_digest=inspect_output_script(tool)), 6)
            self.assertEqual(result.text, text)
            self.assertIn("exceeded its time limit", result.warnings[0])

    async def test_output_limit_rolls_back_a_program_that_keeps_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "unbounded"
            tool.write_text("#!/bin/sh\nexec /usr/bin/yes\n")
            tool.chmod(0o700)
            result = await asyncio.wait_for(process("accepted text", ("script",), script=str(tool),
                                                    script_digest=inspect_output_script(tool)), 3)
            self.assertEqual(result.text, "accepted text")
            self.assertIn("too much text", result.warnings[0])

    @unittest.skipUnless(sys.platform == "darwin", "macOS extended attributes")
    async def test_script_snapshot_preserves_macos_attributes(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "fixture"
            tool.write_text('#!/bin/sh\n/usr/bin/xattr -p local.jaro.fixture "$0"\n')
            tool.chmod(0o700)
            subprocess.run(["/usr/bin/xattr", "-w", "local.jaro.fixture", "fixture value", tool], check=True)
            result = await process("accepted text", ("script",), script=str(tool), script_digest=inspect_output_script(tool))
            self.assertEqual(result.text, "fixture value")

    async def test_cancelled_script_is_reaped(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "fixture"
            tool.write_text('#!/bin/sh\nprintf "%s" "$$" >"$JARO_OUTPUT_SCRIPT.pid"\nexec /bin/sleep 30\n')
            tool.chmod(0o700)
            running = asyncio.create_task(process("words", ("script",), script=str(tool),
                                                  script_digest=inspect_output_script(tool)))
            marker = tool.with_suffix(".pid")
            try:
                async with asyncio.timeout(2):
                    while not marker.is_file() or not marker.stat().st_size:
                        await asyncio.sleep(.01)
                pid = int(marker.read_text())
            finally:
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_failed_transform_chain_keeps_successful_rewrite(self):
        async def rewrite(text):
            return "revised text"

        result = await process("raw text", ("rewrite", "replace", "script"), "revised => modified", rewrite=rewrite)
        self.assertEqual(result.text, "revised text")
        self.assertIn("executable local script", result.warnings[0])

    async def test_cancellation_does_not_deliver_fallback(self):
        started = asyncio.Event()

        async def rewrite(text):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(process("partial take", ("rewrite", "sentence"), rewrite=rewrite))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_rewrite_deadline_defaults_and_validation(self):
        self.assertEqual(parse_profiles("default | rewrite")[0].deadline, 60)
        self.assertEqual(parse_profiles('[{"target":"default","deadline":7}]')[0].deadline, 7)
        for invalid in (0, -1, 61, True, 4.5):
            with self.assertRaisesRegex(ValueError, "between 1 and 60"):
                parse_profiles(json.dumps([{"target": "default", "deadline": invalid}]))
