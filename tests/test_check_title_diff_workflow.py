"""Pin the PR-title workflow to the semantic diff check."""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-title.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_pr_title_workflow_checks_out_the_full_history():
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4" in TEXT
    assert "fetch-depth: 0" in TEXT


def test_pr_title_workflow_runs_the_title_diff_checker_without_shell_interpolation():
    assert 'python scripts/check_title_diff.py "$BASE_SHA" "$PR_TITLE"' in TEXT
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in TEXT
    assert "PR_TITLE: ${{ github.event.pull_request.title }}" in TEXT
    assert 'run: python scripts/check_title_diff.py "${{' not in TEXT


def test_pr_title_workflow_passes_every_fact_the_release_exemption_needs():
    """The exemption fails closed on a missing fact, so a dropped argument stops the train.

    `--head-repo` and `--author` are the two that make it identity rather than a branch name
    anyone may choose, which is why they are asserted individually rather than as one string.
    """
    assert '--head-ref "$HEAD_REF"' in TEXT
    assert '--head-repo "$HEAD_REPO"' in TEXT
    assert '--base-repo "$BASE_REPO"' in TEXT
    assert '--author "$PR_AUTHOR"' in TEXT
    assert "HEAD_REF: ${{ github.head_ref }}" in TEXT
    assert "HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}" in TEXT
    assert "BASE_REPO: ${{ github.repository }}" in TEXT
    assert "PR_AUTHOR: ${{ github.event.pull_request.user.login }}" in TEXT


def test_the_check_reads_the_pull_requests_own_head():
    """The merge commit carries the base branch's new commits, and `base.sha` does not move.

    Diffing one against the other credits this pull request with everything merged into `main`
    since it opened, which fails a correct title for somebody else's change. The job checks
    out the head, where the merge base is the branch point however far `main` has run on.
    """
    assert TEXT.count("ref: ${{ github.event.pull_request.head.sha }}") == 1
    assert TEXT.count("actions/checkout@") == 1
