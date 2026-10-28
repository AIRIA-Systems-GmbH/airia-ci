# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/bump_plan.py against a fake simple index (#94, first slice).

What these tests hold: the planner moves only what a recipient already
declares, to the newest release that is neither yanked nor a pre-release, never
past a ceiling (the recipient's own or its hold), never down, and a page that is
not a package list stops the run instead of reading as "nothing to bump".

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from steps import ROOT

spec = importlib.util.spec_from_file_location("bump_plan", ROOT / "scripts/bump_plan.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


def page(*files, yanked=()):
    def anchor(f):
        mark = ' data-yanked="bad"' if f in yanked else ""
        return f'<a href="/f/{f}"{mark}>{f}</a><br/>'
    return "<html><body>" + "".join(anchor(f) for f in files) + "</body></html>"


def wheel(name, version):
    return f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


def fake_index(versions, **pages):
    """A `get(url)` serving `versions` ({package: [version, ...]}) plus raw `pages`; any other URL is a 404."""
    def get(url):
        package = url.rstrip("/").rsplit("/", 1)[-1]
        if package in pages:
            return pages[package]
        if package in versions:
            return page(*(wheel(package, v) for v in versions[package]))
        raise bump.PlanError(f"cannot read {url}: HTTP Error 404")
    return get


INDEX = {
    "airia-auth": ["0.8.0", "0.9.0", "0.9.1"],
    "airia-control": ["0.7.0", "0.7.4", "0.8.0", "0.9.2"],
    "airia-commons": ["0.12.1"],
    "airia-data": ["0.8.1", "0.8.2"],
}


def pyproject(*deps, groups=(), extras=()):
    text = "[project]\nname = 'x'\ndependencies = [" + ", ".join(repr(d) for d in deps) + "]\n"
    if extras:
        text += "[project.optional-dependencies]\nserver = [" + ", ".join(repr(d) for d in extras) + "]\n"
    if groups:
        text += "[dependency-groups]\ndev = [" + ", ".join(repr(d) for d in groups) + "]\n"
    return text


def lock(**versions):
    return "".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in versions.items())


def recipient(**kw):
    return {"repo": "o/r", "packages": [], **kw}


def run(rec, py, lk, versions=INDEX, **pages):
    rows, notes = bump.plan(rec, py, lk, fake_index(versions, **pages))
    return {r.package: r for r in rows}, notes


class Targets(unittest.TestCase):
    def test_a_plain_bump_goes_to_the_newest_release(self):
        rows, _ = run(recipient(packages=["airia-auth"]), pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        self.assertEqual(rows["airia-auth"][:4], ("airia-auth", "0.9.0", "0.9.1", "bump"))

    def test_a_pin_and_a_floor_both_move_to_the_newest_and_extras_do_not_change_the_name(self):
        rows, _ = run(recipient(packages=["airia-auth", "airia-data"]),
                      pyproject("airia-auth>=0.8.0", "airia-data[postgres]==0.8.1"),
                      lock(**{"airia-auth": "0.8.0", "airia-data": "0.8.1"}))
        self.assertEqual((rows["airia-auth"].target, rows["airia-data"].target), ("0.9.1", "0.8.2"))

    def test_the_recipients_own_ceiling_holds_the_target_and_says_so(self):
        rows, _ = run(recipient(packages=["airia-control"]), pyproject("airia-control>=0.7.0,<0.8.0"),
                      lock(**{"airia-control": "0.7.0"}))
        row = rows["airia-control"]
        self.assertEqual((row.target, row.state), ("0.7.4", "bump"))
        self.assertIn("held at <0.8.0", row.why)
        self.assertIn("newest is 0.9.2", row.why)

    def test_a_hold_in_the_recipient_list_blocks_the_newest_even_when_pyproject_has_no_ceiling(self):
        hold = {"airia-control": {"ceiling": "<0.8.0", "why": "DES-138"}}
        rows, _ = run(recipient(packages=["airia-control"], holds=hold), pyproject("airia-control>=0.7.0"),
                      lock(**{"airia-control": "0.7.4"}))
        self.assertEqual((rows["airia-control"].target, rows["airia-control"].state), ("0.7.4", "current"))
        self.assertIn("held at <0.8.0", rows["airia-control"].why)

    def test_a_ceiling_is_inclusive_only_for_less_or_equal(self):
        rows, _ = run(recipient(packages=["airia-control"]), pyproject("airia-control<=0.8.0"),
                      lock(**{"airia-control": "0.7.0"}))
        self.assertEqual(rows["airia-control"].target, "0.8.0")

    def test_a_package_the_recipient_does_not_declare_is_never_added(self):
        rows, notes = run(recipient(packages=["airia-auth"]), pyproject("airia-auth==0.9.0"),
                          lock(**{"airia-auth": "0.9.0", "airia-data": "0.8.1"}))
        self.assertEqual(list(rows), ["airia-auth"])
        self.assertEqual(notes, [])

    def test_a_name_only_in_the_lock_is_not_moved_and_a_not_moved_name_is_left_alone(self):
        rec = recipient(packages=["airia-auth"], not_moved={"airia-data": "transitive-only"})
        rows, _ = run(rec, pyproject("airia-auth==0.9.0", "airia-data==0.8.1"),
                      lock(**{"airia-auth": "0.9.0", "airia-data": "0.8.1"}))
        self.assertEqual((rows["airia-data"].state, rows["airia-data"].target), ("not-moved", "0.8.1"))
        self.assertEqual(rows["airia-data"].why, "transitive-only")

    def test_a_direct_reference_is_not_moved(self):
        rows, _ = run(recipient(packages=["airia-auth"]), pyproject("airia-auth @ file:///deps/a.whl"),
                      lock(**{"airia-auth": "0.9.0"}))
        self.assertEqual(rows["airia-auth"].state, "not-moved")

    def test_extras_and_groups_count_as_declared_and_other_packages_are_ignored(self):
        rows, _ = run(recipient(packages=["airia-auth", "airia-data"]),
                      pyproject("requests>=2", extras=["airia-auth==0.9.0"], groups=["airia_data==0.8.1"]),
                      lock(**{"airia-auth": "0.9.0", "airia-data": "0.8.1"}))
        self.assertEqual(sorted(rows), ["airia-auth", "airia-data"])

    def test_already_current_is_a_no_op(self):
        rows, _ = run(recipient(packages=["airia-commons"]), pyproject("airia-commons==0.12.1"),
                      lock(**{"airia-commons": "0.12.1"}))
        self.assertEqual((rows["airia-commons"].state, rows["airia-commons"].why), ("current", ""))

    def test_a_version_is_never_lowered(self):
        rows, _ = run(recipient(packages=["airia-auth"]), pyproject("airia-auth>=0.9.0"),
                      lock(**{"airia-auth": "0.9.5"}))
        self.assertEqual((rows["airia-auth"].state, rows["airia-auth"].target), ("kept", "0.9.5"))

    def test_version_order_is_numeric_not_textual(self):
        rows, _ = run(recipient(packages=["airia-auth"]), pyproject("airia-auth>=0.1"), lock(**{"airia-auth": "0.9.0"}),
                      versions={"airia-auth": ["0.9.0", "0.10.0", "0.2.0"]})
        self.assertEqual(rows["airia-auth"].target, "0.10.0")

    def test_the_recipient_list_and_pyproject_disagreeing_is_noted_not_hidden(self):
        _, notes = run(recipient(packages=["airia-auth", "airia-data"]), pyproject("airia-auth==0.9.0", "airia-commons==0.12.1"),
                       lock(**{"airia-auth": "0.9.0", "airia-commons": "0.12.1"}))
        self.assertEqual(len(notes), 2)
        self.assertIn("airia-commons is declared but not in bump-recipients.toml", notes[0])
        self.assertIn("airia-data is listed in bump-recipients.toml but not declared", notes[1])

    def held_count(self, rec, py, lk):
        rows, notes = bump.plan(rec, py, lk, fake_index(INDEX))
        return bump.render("o/r", rows, notes).splitlines()[-1]

    def test_the_summary_counts_a_real_hold_once(self):
        self.assertEqual(self.held_count(recipient(packages=["airia-control"]), pyproject("airia-control>=0.7.0,<0.8.0"),
                                         lock(**{"airia-control": "0.7.0"})), "  1 to move, 1 held")

    def test_a_not_moved_package_is_not_counted_as_held(self):
        rec = recipient(packages=[], not_moved={"airia-data": "find-links"})
        self.assertEqual(self.held_count(rec, pyproject("airia-data==0.8.1"), lock(**{"airia-data": "0.8.1"})),
                         "  0 to move, 0 held")

    def test_a_kept_package_is_not_counted_as_held(self):
        self.assertEqual(self.held_count(recipient(packages=["airia-auth"]), pyproject("airia-auth>=0.9.0"),
                                         lock(**{"airia-auth": "0.9.5"})), "  0 to move, 0 held")

    def test_nothing_to_bump_is_said_with_its_reason(self):
        self.assertEqual(bump.plan(recipient(), None, None), ([], ["no root pyproject.toml: nothing to bump"]))
        self.assertEqual(bump.plan(recipient(), pyproject("django>=5"), None),
                         ([], ["no airia-* declaration: nothing to bump"]))


class Index(unittest.TestCase):
    def releases(self, *files, yanked=()):
        return bump.releases("airia-auth", lambda url: page(*files, yanked=yanked))

    def test_yanked_and_pre_releases_are_never_the_latest(self):
        got = self.releases(wheel("airia-auth", "0.9.0"), wheel("airia-auth", "0.9.1"), wheel("airia-auth", "0.9.2rc1"),
                            wheel("airia-auth", "1.0.0.dev3"), "airia_auth-0.9.3.tar.gz",
                            yanked=(wheel("airia-auth", "0.9.1"), "airia_auth-0.9.3.tar.gz"))
        self.assertEqual(got, ["0.9.0"])

    def test_a_release_with_one_yanked_file_and_one_live_file_is_live(self):
        got = self.releases(wheel("airia-auth", "0.9.0"), "airia_auth-0.9.0.tar.gz", yanked=("airia_auth-0.9.0.tar.gz",))
        self.assertEqual(got, ["0.9.0"])

    def test_an_empty_yanked_anchor_does_not_lend_its_flag_to_later_text(self):
        parser = bump.Anchors()
        parser.feed('<a href="/x" data-yanked="gone"></a>stray text<a href="/f">' + wheel("airia-auth", "0.9.0") + "</a>")
        self.assertEqual(parser.files, [(wheel("airia-auth", "0.9.0"), False)])

    def test_files_of_another_package_do_not_count(self):
        with self.assertRaisesRegex(bump.PlanError, "no released, unyanked version of airia-auth"):
            self.releases(wheel("airia-authx", "1.0.0"))

    def test_an_empty_or_non_index_page_fails_loudly(self):
        for body in ("", "<html><body>Welcome</body></html>"):
            with self.subTest(body=body), self.assertRaisesRegex(bump.PlanError, "not a package list"):
                bump.releases("airia-auth", lambda url, b=body: b)

    def test_an_unknown_file_name_fails_loudly(self):
        with self.assertRaisesRegex(bump.PlanError, "not a wheel or sdist"):
            self.releases("../")

    def test_only_yanked_releases_fail_loudly(self):
        with self.assertRaisesRegex(bump.PlanError, "no released, unyanked"):
            self.releases(wheel("airia-auth", "0.9.0"), yanked=(wheel("airia-auth", "0.9.0"),))

    def test_the_url_asked_is_the_normalised_package_under_the_index(self):
        seen = []
        bump.releases("Airia_Auth", lambda url: seen.append(url) or page(wheel("airia-auth", "1.0")), "https://i/simple")
        self.assertEqual(seen, ["https://i/simple/airia-auth/"])

    def test_a_package_the_index_does_not_serve_stops_the_plan(self):
        with self.assertRaisesRegex(bump.PlanError, "404"):
            run(recipient(packages=["airia-nope"]), pyproject("airia-nope==1.0"), lock(**{"airia-nope": "1.0"}))

    def test_a_malformed_page_for_one_package_stops_the_whole_plan(self):
        with self.assertRaises(bump.PlanError):
            run(recipient(packages=["airia-auth", "airia-data"]), pyproject("airia-auth==0.9.0", "airia-data==0.8.1"),
                lock(**{"airia-auth": "0.9.0", "airia-data": "0.8.1"}), **{"airia-data": "<html>oops</html>"})

    def test_the_real_fetch_reports_an_unreachable_index_as_a_plan_error(self):
        with self.assertRaisesRegex(bump.PlanError, "cannot read"):
            bump.fetch("http://127.0.0.1:9/simple/airia-auth/")
        with self.assertRaisesRegex(bump.PlanError, "cannot read"):
            bump.fetch("not a url")


class Inputs(unittest.TestCase):
    def test_a_lock_that_misses_a_declared_package_fails(self):
        with self.assertRaisesRegex(bump.PlanError, "uv.lock has no airia-auth"):
            run(recipient(packages=["airia-auth"]), pyproject("airia-auth==0.9.0"), lock())

    def test_a_missing_lock_fails(self):
        with self.assertRaisesRegex(bump.PlanError, "uv.lock is missing"):
            bump.plan(recipient(packages=["airia-auth"]), pyproject("airia-auth==0.9.0"), None, fake_index(INDEX))

    def test_a_specifier_the_planner_cannot_read_fails_instead_of_being_guessed(self):
        for spec in ("airia-auth~=0.9", "airia-auth!=0.9.1", "airia-auth==0.9.*"):
            with self.subTest(spec=spec), self.assertRaisesRegex(bump.PlanError, "unsupported specifier"):
                run(recipient(packages=["airia-auth"]), pyproject(spec), lock(**{"airia-auth": "0.9.0"}))

    def test_an_unreadable_requirement_fails(self):
        with self.assertRaisesRegex(bump.PlanError, "cannot read the requirement"):
            bump.declared({"project": {"dependencies": ["==1"]}})

    def test_a_marker_is_ignored_and_an_include_group_is_not_a_requirement(self):
        got = bump.declared({"project": {"dependencies": ["airia-auth==0.9.0; python_version >= '3.12'"]},
                             "dependency-groups": {"all": [{"include-group": "dev"}]}})
        self.assertEqual(got, {"airia-auth": ["==0.9.0"]})


class Recipients(unittest.TestCase):
    def test_the_list_holds_what_issue_94_records(self):
        by = {r["repo"]: r for r in bump.recipients()["recipient"]}
        self.assertEqual(len(by), 8)
        self.assertEqual(by["AIRIA-Systems-GmbH/AIRIA-process"]["holds"]["airia-control"]["ceiling"], "<0.8.0")
        self.assertIn("airia-data", by["tumma72/CryptoStudio"]["not_moved"])
        self.assertNotIn("airia-data", by["tumma72/CryptoStudio"]["packages"])
        self.assertIn("airia-surrealdb", by["AIRIA-Systems-GmbH/AIRIA-DevTools"]["not_moved"])
        self.assertEqual(by["tumma72/agile42-TCC"]["packages"], [])
        self.assertNotIn("airia-plugins", by["tumma72/Portfolio-Dashboard"]["packages"])

    def test_every_entry_is_well_formed(self):
        listed = bump.recipients()
        for r in listed["recipient"]:
            self.assertTrue(set(r) <= {"repo", "note", "packages", "not_moved", "holds"}, r)
            self.assertTrue(all(p.startswith("airia-") for p in r["packages"]), r)
            self.assertFalse(set(r["packages"]) & set(r.get("not_moved", {})), r)
            for p, hold in r.get("holds", {}).items():
                self.assertIn(p, r["packages"])
                self.assertTrue(hold["ceiling"].startswith("<") and hold["why"])
        self.assertTrue(all(e["why"] for e in listed["excluded"]))

    def test_a_repository_the_caller_scan_finds_is_listed_or_excluded_with_a_reason(self):
        # What consumers.py finds today: ParlAIment is not among them, and that is why this list is its own.
        scan = ["AIRIA-Systems-GmbH/AIRIA-framework", "AIRIA-Systems-GmbH/Second-Memory-System",
                "AIRIA-Systems-GmbH/AIRIA-process", "tumma72/CryptoStudio"]
        self.assertEqual(bump.unlisted(scan), [])
        self.assertEqual(bump.unlisted([*scan, "o/new-caller"]), ["o/new-caller"])


class Cli(unittest.TestCase):
    def call(self, argv, get):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bump.main(argv, get)
        return code, out.getvalue(), err.getvalue()

    def files(self, py, lk):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in tmp.iterdir()] and tmp.rmdir())
        (tmp / "pyproject.toml").write_text(py)
        (tmp / "uv.lock").write_text(lk)
        return ["--pyproject", str(tmp / "pyproject.toml"), "--lock", str(tmp / "uv.lock")], tmp

    def test_it_prints_the_plan_and_writes_nothing(self):
        files, tmp = self.files(pyproject("airia-control>=0.7.0,<0.8.0", "airia-auth==0.9.0"),
                                lock(**{"airia-control": "0.7.0", "airia-auth": "0.9.0"}))
        before = sorted(p.name for p in tmp.iterdir())
        code, out, _ = self.call(["--dry-run", "--recipient", "AIRIA-Systems-GmbH/AIRIA-process", *files],
                                 fake_index(INDEX))
        self.assertEqual(code, 0)
        self.assertIn("airia-control\t0.7.0\t0.7.4\tbump\theld at <0.8.0", out)
        self.assertIn("airia-auth\t0.9.0\t0.9.1\tbump", out)
        self.assertIn("2 to move, 1 held", out)
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), before)

    def test_it_will_not_run_without_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bump.main(["--recipient", "o/r", "--pyproject", "a", "--lock", "b"])

    def test_an_unlisted_recipient_and_a_bad_index_both_exit_2_with_nothing_planned(self):
        files, _ = self.files(pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        code, out, err = self.call(["--dry-run", "--recipient", "o/unknown", *files], fake_index(INDEX))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not in bump-recipients.toml; nothing was planned", err)
        code, out, err = self.call(["--dry-run", "--recipient", "AIRIA-Systems-GmbH/ParlAIment", *files],
                                   lambda url: "<html></html>")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not a package list", err)

    def test_a_recipient_without_files_is_skipped_with_its_reason(self):
        code, out, _ = self.call(["--dry-run", "--recipient", "tumma72/agile42-TCC", "--pyproject", "/nope/p.toml",
                                  "--lock", "/nope/uv.lock"], fake_index(INDEX))
        self.assertEqual(code, 0)
        self.assertIn("no root pyproject.toml: nothing to bump", out)
        self.assertIn("0 to move, 0 held", out)


if __name__ == "__main__":
    unittest.main()
