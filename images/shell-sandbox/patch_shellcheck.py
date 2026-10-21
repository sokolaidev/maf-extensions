"""Add an observation callback to the pinned ShellCheck annotation parser."""

from pathlib import Path


def replace(path: Path, before: str, after: str) -> None:
    """Refuse a source upgrade whose parser no longer matches the reviewed patch."""
    text = path.read_text()
    if text.count(before) != 1:
        raise ValueError(f"unexpected ShellCheck source: {path.name}")
    path.write_text(text.replace(before, after))


if __name__ == "__main__":
    root = Path("/build/shellcheck-0.11.0/src/ShellCheck")
    (root.parent / "Paths_ShellCheck.hs").write_text(
        "module Paths_ShellCheck (version) where\n"
        "import Data.Version (makeVersion)\n"
        "version = makeVersion [0,11,0]\n"
    )
    replace(
        root / "Interface.hs",
        "    siGetConfig :: String -> m (Maybe (FilePath, String))\n",
        "    siGetConfig :: String -> m (Maybe (FilePath, String)),\n"
        "    siAnnotation :: Position -> [Annotation] -> m (),\n"
        "    siIgnoreSuppressions :: Bool\n",
    )
    replace(
        root / "Interface.hs",
        "        siGetConfig = \\_ -> return Nothing\n",
        "        siGetConfig = \\_ -> return Nothing,\n"
        "        siAnnotation = \\_ _ -> return (),\n"
        "        siIgnoreSuppressions = False\n",
    )
    replace(
        root / "Parser.hs",
        "        many linewhitespace\n        return annotations\n",
        "        sys <- Mr.asks systemInterface\n"
        "        system $ siAnnotation sys (posToPos keyPos) annotations\n"
        "        many linewhitespace\n"
        "        return $ if siIgnoreSuppressions sys\n"
        "            then filter keep annotations else annotations\n"
        "      where\n"
        "        keep (DisableComment _ _) = False\n"
        "        keep _ = True\n",
    )
