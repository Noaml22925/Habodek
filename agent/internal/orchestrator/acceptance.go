package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"habodek/internal/core/brain"
)

// Acceptance is how developers received one category of posted findings in one
// repository, from the verified thread outcomes (design §6.10):
//
//   - accepted: fixed or fix_incomplete (they acted on it), or stands (they disputed it
//     and the re-check, verified by the second model, upheld it);
//   - rejected: withdrawn (they showed it was wrong and the bot conceded);
//   - neutral: unclear or need_info; open: no outcome yet. Neither counts.
type Acceptance struct {
	Category string  `json:"category"`
	Posted   int     `json:"posted"`
	Open     int     `json:"open"`
	Accepted int     `json:"accepted"`
	Rejected int     `json:"rejected"`
	Neutral  int     `json:"neutral"`
	Rate     float64 `json:"acceptance"` // accepted / (accepted + rejected); 0 with no outcomes
}

// Outcomes are the decided threads the rate is computed from.
func (a Acceptance) Outcomes() int { return a.Accepted + a.Rejected }

// BarConfig decides when a category's bar is raised.
type BarConfig struct {
	Window        int     // most recent posted findings per category considered (default 50)
	MinOutcomes   int     // decided threads needed before acting (default 5)
	MajorBelow    float64 // acceptance below this: post major and above (default 0.5)
	CriticalBelow float64 // acceptance below this: post critical only (default 0.25)
	Disabled      bool
}

func (c BarConfig) withDefaults() BarConfig {
	if c.Window <= 0 {
		c.Window = 50
	}
	if c.MinOutcomes <= 0 {
		c.MinOutcomes = 5
	}
	if c.MajorBelow <= 0 {
		c.MajorBelow = 0.5
	}
	if c.CriticalBelow <= 0 {
		c.CriticalBelow = 0.25
	}
	return c
}

func primaryCategory(category string) string {
	first := strings.TrimSpace(strings.SplitN(category, ",", 2)[0])
	if first == "" {
		return "uncategorised"
	}
	return first
}

// Acceptance computes per-category acceptance for a project over the most recent
// Window posted findings of each category, sorted by category.
func (s *Store) Acceptance(ctx context.Context, projectID int64, cfg BarConfig) ([]Acceptance, error) {
	cfg = cfg.withDefaults()
	rows, err := s.db.QueryContext(ctx, s.rebind(`SELECT verdict, finding_json FROM hbk_findings
		WHERE project_id = ? ORDER BY updated_at DESC`), projectID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	by := map[string]*Acceptance{}
	for rows.Next() {
		var verdict, raw string
		if err := rows.Scan(&verdict, &raw); err != nil {
			return nil, err
		}
		var f brain.Finding
		_ = json.Unmarshal([]byte(raw), &f)
		cat := primaryCategory(f.Category)
		a := by[cat]
		if a == nil {
			a = &Acceptance{Category: cat}
			by[cat] = a
		}
		if a.Posted >= cfg.Window {
			continue
		}
		a.Posted++
		switch verdict {
		case "fixed", "fix_incomplete", "stands":
			a.Accepted++
		case "withdrawn":
			a.Rejected++
		case "":
			a.Open++
		default:
			a.Neutral++
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]Acceptance, 0, len(by))
	for _, a := range by {
		if n := a.Outcomes(); n > 0 {
			a.Rate = float64(a.Accepted) / float64(n)
		}
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Category < out[j].Category })
	return out, nil
}

// Bars turns acceptance into raised bars: a category developers keep rejecting posts
// only its more severe findings until its acceptance recovers.
func Bars(stats []Acceptance, cfg BarConfig) []brain.CategoryBar {
	cfg = cfg.withDefaults()
	if cfg.Disabled {
		return nil
	}
	var bars []brain.CategoryBar
	for _, a := range stats {
		if a.Outcomes() < cfg.MinOutcomes {
			continue
		}
		min := ""
		switch {
		case a.Rate < cfg.CriticalBelow:
			min = "critical"
		case a.Rate < cfg.MajorBelow:
			min = "major"
		}
		if min != "" {
			bars = append(bars, brain.CategoryBar{Category: a.Category, MinSeverity: min,
				Acceptance: a.Rate, Outcomes: a.Outcomes()})
		}
	}
	return bars
}

// policy for a review job, logged whenever a bar is raised.
func (w *Worker) policy(ctx context.Context, projectID int64, projectPath string) *brain.Policy {
	stats, err := w.Store.Acceptance(ctx, projectID, w.Config.Bars)
	if err != nil {
		slog.Warn("acceptance", "project", projectPath, "error", err)
		return nil
	}
	bars := Bars(stats, w.Config.Bars)
	if len(bars) == 0 {
		return nil
	}
	for _, b := range bars {
		slog.Info("bar raised", "project", projectPath, "category", b.Category,
			"acceptance", fmt.Sprintf("%.0f%%", b.Acceptance*100), "outcomes", b.Outcomes,
			"posts", b.MinSeverity+"+")
	}
	return &brain.Policy{Bars: bars}
}

// Report is a project's acceptance and the bars it raises (GET /internal/metrics).
type Report struct {
	ProjectID  int64               `json:"project_id"`
	Categories []Acceptance        `json:"categories"`
	Bars       []brain.CategoryBar `json:"bars"`
}

// MetricsHandler serves GET /internal/metrics?project=<id> as JSON.
func MetricsHandler(store *Store, cfg BarConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		pid, err := strconv.ParseInt(r.URL.Query().Get("project"), 10, 64)
		if err != nil {
			http.Error(w, "project=<id> is required", http.StatusBadRequest)
			return
		}
		stats, err := store.Acceptance(r.Context(), pid, cfg)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(Report{ProjectID: pid, Categories: stats, Bars: Bars(stats, cfg)})
	}
}
