"""Pin the PR-title workflow to the semantic diff check."""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-title.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_pr_title_workflow_checks_out_the_full_history():
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4" in TEXT
    assert "fetch-depth: 0" in TEXT


def test_pr_title_workflow_runs_the_title_diff_checker_without_shell_interpolation():
    assert 'python scripts/check_title_diff.py "$BASE_SHA" "$PR_TITLE" "$HEAD_REF"' in TEXT
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in TEXT
    assert "PR_TITLE: ${{ github.event.pull_request.title }}" in TEXT
    assert "HEAD_REF: ${{ github.head_ref }}" in TEXT
    assert 'run: python scripts/check_title_diff.py "${{' not in TEXT


def test_pr_title_workflow_passes_the_head_ref_so_release_pull_requests_are_skipped():
    """The script exempts release-please's branches, and only if it is told the branch.

    Asserted as its own test rather than folded above, because the two failures differ: the
    interpolation one is a shell-injection guard, and this one is the difference between the
    release train moving and every Release PR reporting a failure nobody may fix — the title
    is generated, so retitling to satisfy the check is not available.
    """
    assert '"$HEAD_REF"' in TEXT
    assert "github.head_ref" in TEXT
