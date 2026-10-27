// Regenerate the external dependency imports from a verified upstream proxy checkout.
package main

import (
	"fmt"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: proxy_dependency_imports UPSTREAM_DIRECTORY")
		os.Exit(2)
	}
	imports := map[string]bool{}
	err := filepath.WalkDir(os.Args[1], func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") {
			return nil
		}
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		for _, spec := range file.Imports {
			name, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				return err
			}
			if strings.Contains(strings.Split(name, "/")[0], ".") && !strings.HasPrefix(name, "github.com/ironsh/iron-proxy/") {
				imports[name] = true
			}
		}
		return nil
	})
	if err != nil {
		panic(err)
	}
	names := []string{}
	for name := range imports {
		names = append(names, name)
	}
	sort.Strings(names)
	fmt.Println("// Dependency imports retain the pinned upstream production and test graph for updates.")
	fmt.Println("package dependencies\n\nimport (")
	for _, name := range names {
		fmt.Printf("\t_ %q\n", name)
	}
	fmt.Println(")")
}
