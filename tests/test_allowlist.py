from gcx import allowlist

BASE = "display_name: awikner midway3 login (pilot)\n"


def test_render_adds_sorted_block():
    out = allowlist.render(BASE, {"b-uuid", "a-uuid"})
    assert out == BASE + ("# gcx: only these registered functions may run (gcx allowlist).\n"
                          "allowed_functions:\n  - a-uuid\n  - b-uuid\n")


def test_render_replaces_existing_block_and_keeps_other_keys():
    old = allowlist.render(BASE + "public: false\n", {"old"})
    new = allowlist.render(old, {"new"})
    assert "old" not in new.split("allowed_functions:")[1] and "  - new" in new
    assert "public: false" in new and new.count("allowed_functions:") == 1


def test_render_off_restores_original():
    assert allowlist.render(allowlist.render(BASE, {"x"}), None) == BASE
