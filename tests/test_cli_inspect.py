from applypilot.cli import build_parser


def test_inspect_selected_is_explicit_and_limited():
    args = build_parser().parse_args([
        "inspect", "--input", "snapshot.json", "--selected", "--preset", "go-backend", "--limit", "3"
    ])

    assert args.selected is True
    assert args.preset == "go-backend"
    assert args.limit == 3
