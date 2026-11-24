package sandbox

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ScriptVersion must equal SCRIPT_VERSION in envprep.py (checked by a test).
const ScriptVersion = "1"

// requirementFiles and keyFiles mirror REQUIREMENT_FILES and KEY_FILES in envprep.py.
var requirementFiles = []string{
	"requirements.txt", "requirements-dev.txt", "requirements-test.txt",
	"requirements_dev.txt", "requirements_test.txt", "dev-requirements.txt",
	"test-requirements.txt",
}

var keyFiles = append(append([]string{}, requirementFiles...),
	"pyproject.toml", "setup.cfg", "setup.py", "uv.lock", "poetry.lock", "pdm.lock")

// CacheKey is envprep.cache_key: sha256 over the dependency files of src (name and
// content), the recipe text and the interpreter tag. A shared fixture
// (testdata/keyrepo) pins both implementations to the same value.
func CacheKey(src, recipeText, tag string) (string, error) {
	h := sha256.New()
	fmt.Fprintf(h, "habodek-envprep/%s\n", ScriptVersion)
	fmt.Fprintf(h, "tag:%s\n", tag)
	var names []string
	for _, n := range keyFiles {
		if st, err := os.Stat(filepath.Join(src, n)); err == nil && st.Mode().IsRegular() {
			names = append(names, n)
		}
	}
	if entries, err := os.ReadDir(filepath.Join(src, "requirements")); err == nil {
		for _, e := range entries {
			if strings.HasSuffix(e.Name(), ".txt") {
				names = append(names, "requirements/"+e.Name())
			}
		}
	}
	sort.Strings(names)
	for _, n := range names {
		data, err := os.ReadFile(filepath.Join(src, filepath.FromSlash(n)))
		if err != nil {
			return "", err
		}
		fmt.Fprintf(h, "file:%s:%d\n", n, len(data))
		h.Write(data)
	}
	fmt.Fprintf(h, "recipe:%d\n", len(recipeText))
	h.Write([]byte(recipeText))
	return hex.EncodeToString(h.Sum(nil))[:32], nil
}
