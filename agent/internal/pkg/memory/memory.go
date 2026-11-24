// Package memory is the control plane's view of the habodek-memory repository (M10):
// which commit a job reads, whether a project has a repo card, and the
// reverse-dependency index built from every project's facts.json. Writing is the
// brain's (`habodek-brain memory-apply`); this package only serialises the writers.
package memory

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// Facts is projects/<path>/facts.json, as the cartographer writes it.
type Facts struct {
	Project       string            `json:"project"`
	GitLabID      int64             `json:"gitlab_id"`
	Package       string            `json:"package"`
	DefaultBranch string            `json:"default_branch"`
	SHA           string            `json:"sha"`
	MappedAt      string            `json:"mapped_at"`
	Pins          map[string]string `json:"pins"`
}

// Store is the memory repository on disk.
type Store struct {
	Dir string
	mu  sync.Mutex
}

func New(dir string) *Store { return &Store{Dir: dir} }

func projectDir(dir, project string) string {
	return filepath.Join(dir, "projects", filepath.FromSlash(strings.Trim(project, "/")))
}

// Head is the commit jobs should read ("" for a repository with no commits yet).
func (s *Store) Head(ctx context.Context) string {
	out, err := exec.CommandContext(ctx, "git", "-C", s.Dir, "rev-parse", "--verify", "-q",
		"HEAD^{commit}").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// HasCard reports whether the cartographer has mapped the project.
func (s *Store) HasCard(project string) bool {
	_, err := os.Stat(filepath.Join(projectDir(s.Dir, project), "card.md"))
	return err == nil
}

// Facts returns a project's facts, if it has been mapped.
func (s *Store) Facts(project string) (*Facts, bool) {
	data, err := os.ReadFile(filepath.Join(projectDir(s.Dir, project), "facts.json"))
	if err != nil {
		return nil, false
	}
	var f Facts
	if json.Unmarshal(data, &f) != nil {
		return nil, false
	}
	return &f, true
}

func norm(pkg string) string { return strings.ReplaceAll(strings.ToLower(pkg), "_", "-") }

// Consumers are the mapped projects whose default branch pins the package the given
// project publishes (§3.5 step 4), most recently mapped first.
func (s *Store) Consumers(project string) []Facts {
	own, ok := s.Facts(project)
	if !ok || own.Package == "" {
		return nil
	}
	var out []Facts
	_ = filepath.WalkDir(filepath.Join(s.Dir, "projects"), func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || d.Name() != "facts.json" {
			return nil
		}
		data, rerr := os.ReadFile(p)
		var f Facts
		if rerr != nil || json.Unmarshal(data, &f) != nil || f.Project == project {
			return nil
		}
		for pkg := range f.Pins {
			if norm(pkg) == norm(own.Package) {
				out = append(out, f)
				break
			}
		}
		return nil
	})
	sort.Slice(out, func(i, j int) bool { return out[i].MappedAt > out[j].MappedAt })
	return out
}

// Write runs fn (the brain's memory-apply) with the repository locked.
func (s *Store) Write(fn func() (string, error)) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return fn()
}
