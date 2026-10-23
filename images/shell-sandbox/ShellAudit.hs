{-# LANGUAGE OverloadedStrings #-}
-- SPDX-License-Identifier: GPL-3.0-or-later
-- The adapter uses the pinned ShellCheck parser and never reads the guest filesystem.
module Main where

import Control.Monad (foldM, forM, void)
import Data.Aeson
import Data.IORef
import Data.List (find, nub, sortOn, (\\))
import Data.Maybe (isJust, fromMaybe, mapMaybe)
import qualified Data.ByteString.Lazy.Char8 as BL
import qualified Data.Map.Strict as Map
import ShellCheck.AST
import ShellCheck.Checker (checkScript)
import ShellCheck.Interface
import ShellCheck.Parser (parseScript)
import qualified System.FilePath.Posix as P

data Source = Source { name :: String, text :: String, dialect :: String }
instance FromJSON Source where
    parseJSON = withObject "source" $ \v -> Source <$> v .: "path" <*> v .: "text" <*> v .: "dialect"
data Request = Request [Source]
instance FromJSON Request where
    parseJSON = withObject "request" $ \v -> Request <$> v .: "files"

shell :: String -> Shell
shell "bash" = Bash
shell "sh" = Sh
shell "dash" = Dash
shell _ = error "unsupported dialect"

severity :: Severity -> String
severity ErrorC = "error"
severity WarningC = "warning"
severity InfoC = "info"
severity StyleC = "style"

diagnostic :: PositionedComment -> Value
diagnostic item = object [
    "file" .= posFile pos, "line" .= posLine pos, "column" .= posColumn pos,
    "rule" .= ("SC" ++ show (cCode c)), "severity" .= severity (cSeverity c),
    "message" .= cMessage c]
  where pos = pcStartPos item
        c = pcComment item

position :: Position -> Value
position p = object ["line" .= posLine p, "column" .= posColumn p]

before :: Position -> Position -> Bool
before a b = posFile a == posFile b && (posLine a, posColumn a) <= (posLine b, posColumn b)

-- Manifest sources have no symlinks, so parent components resolve lexically.
manifestPath :: FilePath -> Maybe FilePath
manifestPath value
    | P.isAbsolute value = Nothing
    | otherwise = P.joinPath . reverse <$> foldM step [] (P.splitDirectories value)
  where
    step stack "." = Just stack
    step [] ".." = Nothing
    step (_:rest) ".." = Just rest
    step stack part = Just (part:stack)

main :: IO ()
main = do
    raw <- BL.getContents
    Request sources <- either fail return (eitherDecode raw)
    let files = Map.fromList [(name s, text s) | s <- sources]
    reports <- forM sources $ \source -> do
        audit <- newIORef []
        let resolve current _ paths target = return $ fromMaybe target $ find (`Map.member` files) $
                nub $ mapMaybe manifestPath $ expand current target :
                    [expand current p P.</> target | p <- paths, not (P.isAbsolute p)]
            expand current p = case P.splitDirectories p of
                ("SCRIPTDIR":rest) -> P.joinPath (P.takeDirectory current:rest)
                _ -> p
            system = (newSystemInterface :: SystemInterface IO) {
                siGetConfig = \_ -> return Nothing,
                siReadFile = \_ path -> return $
                    if path == "/dev/null" then Right "" else
                    maybe (Left "source is outside the manifest") Right (Map.lookup path files),
                siFindSource = resolve,
                siAnnotation = \p annotations -> modifyIORef' audit ((p, annotations):)
            }
            parsedSpec = newParseSpec {
                psFilename = name source, psScript = text source,
                psIgnoreRC = True, psShellTypeOverride = Just (shell (dialect source))
            }
        parsed <- parseScript system parsedSpec
        declarations <- nub <$> readIORef audit
        spans <- newIORef []
        case prRoot parsed of
            Nothing -> return ()
            Just root -> void $ doAnalysis (\token -> case token of
                T_Annotation ident _ child -> case
                    (Map.lookup ident (prTokenPositions parsed), Map.lookup (getId child) (prTokenPositions parsed)) of
                        (Just annotationSpan, Just scopeSpan) -> modifyIORef' spans ((annotationSpan, scopeSpan):)
                        _ -> return ()
                _ -> return ()) root
        scopeSpans <- readIORef spans
        let checks = emptyCheckSpec {
            csFilename = name source, csScript = text source,
            csIgnoreRC = True, csShellTypeOverride = Just (shell (dialect source)),
            csExtendedAnalysis = Just True, csMinSeverity = StyleC,
            csCheckSourced = True
            }
        result <- checkScript system checks
        baseline <- checkScript (system { siIgnoreSuppressions = True }) checks
        let local = [(p, as) | (p, as) <- declarations, posFile p == name source]
            suppressed = mapMaybe (suppression scopeSpans) local
            conflicting = any (any (conflict (dialect source)) . snd) local
        return $ object [
            "path" .= name source, "parsed" .= isJust (prRoot parsed),
            "policy_conflict" .= conflicting,
            "diagnostics" .= map diagnostic (crComments result),
            "suppressed_diagnostics" .= map diagnostic (crComments baseline \\ crComments result),
            "suppressions" .= sortOn show suppressed]
    BL.putStrLn $ encode $ object ["protocol" .= (1 :: Int), "files" .= reports]

conflict :: String -> Annotation -> Bool
conflict selected (ShellOverride value) = selected /= value
conflict _ (ExtendedAnalysis False) = True
conflict _ (ExternalSources True) = True
conflict _ (EnableComment _) = True
conflict _ _ = False

suppression :: [((Position, Position), (Position, Position))] -> (Position, [Annotation]) -> Maybe Value
suppression spans (p, annotations)
    | null ranges = Nothing
    | otherwise = Just $ object [
        "line" .= posLine p, "column" .= posColumn p,
        "rules" .= [object ["first" .= first, "end_exclusive" .= end] | (first, end) <- ranges],
        "scope" .= fmap (\(_, (start, end)) -> object ["start" .= position start, "end" .= position end]) matching]
  where
    ranges = [(first, end) | DisableComment first end <- annotations]
    matching = find (\((start, end), _) -> before start p && before p end) spans
