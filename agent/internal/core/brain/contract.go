// Package brain runs the Python brain (habodek-brain) as one subprocess per job, over
// the JSON contract in habodek/brain/contract.py: job.json in, result.json out.
//
// These types mirror that contract. The Python side rejects unknown fields, so a field
// added here must be added there too; testdata/ holds golden files both sides check.
package brain

// ContractVersion must match CONTRACT_VERSION in habodek/brain/contract.py.
const ContractVersion = 1

type MergeRequest struct {
	Project      string   `json:"project"`
	IID          int64    `json:"iid"`
	Title        string   `json:"title,omitempty"`
	Description  string   `json:"description,omitempty"`
	Author       string   `json:"author,omitempty"`
	Labels       []string `json:"labels,omitempty"`
	SourceBranch string   `json:"source_branch,omitempty"`
	TargetBranch string   `json:"target_branch,omitempty"`
	WebURL       string   `json:"web_url,omitempty"`
	Draft        bool     `json:"draft,omitempty"`
}

type Revisions struct {
	BaseSHA  string `json:"base_sha"`
	HeadSHA  string `json:"head_sha"`
	StartSHA string `json:"start_sha,omitempty"`
}

type DependencyTree struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Path    string `json:"path"`
	Side    string `json:"side,omitempty"`
}

type Workspace struct {
	Head         string            `json:"head"`
	Base         string            `json:"base,omitempty"`
	Repo         string            `json:"repo,omitempty"`
	Dependencies []DependencyTree  `json:"dependencies,omitempty"`
	Venvs        []string          `json:"venvs,omitempty"`
	OtherRepos   map[string]string `json:"other_repos,omitempty"`
}

type EvidenceLink struct {
	Path     string `json:"path"`
	Line     int    `json:"line"`
	Quote    string `json:"quote"`
	Claim    string `json:"claim"`
	Revision string `json:"revision,omitempty"`
	Repo     string `json:"repo,omitempty"`
}

type Anchor struct {
	Path    string `json:"path"`
	Line    int    `json:"line"`
	Side    string `json:"side,omitempty"`
	Snippet string `json:"snippet,omitempty"`
}

type Verification struct {
	Verifier string         `json:"verifier"`
	Model    string         `json:"model"`
	Outcome  string         `json:"outcome"`
	Reason   string         `json:"reason,omitempty"`
	Evidence []EvidenceLink `json:"evidence,omitempty"`
}

type Finding struct {
	ID            string         `json:"id"`
	Title         string         `json:"title"`
	Severity      string         `json:"severity,omitempty"`
	Category      string         `json:"category,omitempty"`
	Claim         string         `json:"claim"`
	Scenario      string         `json:"scenario,omitempty"`
	Fix           string         `json:"fix,omitempty"`
	Anchor        Anchor         `json:"anchor"`
	Chain         []EvidenceLink `json:"chain,omitempty"`
	HeadSHA       string         `json:"head_sha,omitempty"`
	Verifications []Verification `json:"verifications,omitempty"`
	Status        string         `json:"status,omitempty"`
	DroppedAt     *string        `json:"dropped_at,omitempty"`
	DropReason    *string        `json:"drop_reason,omitempty"`
}

type Note struct {
	Author    string `json:"author"`
	Body      string `json:"body"`
	CreatedAt string `json:"created_at,omitempty"`
	IsBot     bool   `json:"is_bot,omitempty"`
}

type ModelRouting struct {
	Roles map[string]string `json:"roles,omitempty"`
}

// Job is either a review job or a thread job (Kind discriminates).
type Job struct {
	Contract      int          `json:"contract"`
	Kind          string       `json:"kind"` // review | thread
	JobID         string       `json:"job_id"`
	MR            MergeRequest `json:"mr"`
	Revisions     Revisions    `json:"revisions"`
	Workspace     Workspace    `json:"workspace"`
	Priority      string       `json:"priority,omitempty"`
	RequestBudget *int         `json:"request_budget,omitempty"`
	MemoryPath    *string      `json:"memory_path,omitempty"`
	Models        ModelRouting `json:"models"`
	// review only
	MaxComments  int       `json:"max_comments,omitempty"`
	OpenFindings []Finding `json:"open_findings,omitempty"`
	// thread only
	Event   string   `json:"event,omitempty"` // reply | push
	Finding *Finding `json:"finding,omitempty"`
	Notes   []Note   `json:"notes,omitempty"`
}

// Envelope is the shape of job.json.
type Envelope struct {
	Job Job `json:"job"`
}

type Comment struct {
	FindingID string `json:"finding_id"`
	Path      string `json:"path"`
	Line      int    `json:"line"`
	Side      string `json:"side"`
	Body      string `json:"body"`
	Marker    string `json:"marker"`
}

type ThreadVerdict struct {
	Verdict         string         `json:"verdict"`
	Body            string         `json:"body"`
	Remaining       []EvidenceLink `json:"remaining"`
	ConvincedBy     []EvidenceLink `json:"convinced_by"`
	LessonCandidate *string        `json:"lesson_candidate"`
}

type StageStat struct {
	Stage    string  `json:"stage"`
	Requests int     `json:"requests"`
	Agents   int     `json:"agents"`
	Seconds  float64 `json:"seconds"`
	Notes    string  `json:"notes"`
}

// Result is result.json.
type Result struct {
	Contract        int            `json:"contract"`
	JobID           string         `json:"job_id"`
	RunID           string         `json:"run_id"`
	Kind            string         `json:"kind"`
	Status          string         `json:"status"` // ok | skipped | failed
	Reason          string         `json:"reason"`
	Comments        []Comment      `json:"comments"`
	Summary         string         `json:"summary"`
	Findings        []Finding      `json:"findings"`
	Thread          *ThreadVerdict `json:"thread"`
	NotInvestigated []string       `json:"not_investigated"`
	Stats           []StageStat    `json:"stats"`
	Requests        int            `json:"requests"`
	LLM             map[string]any `json:"llm"`
}

// Posted returns the findings the brain decided to post.
func (r *Result) Posted() []Finding {
	var out []Finding
	for _, f := range r.Findings {
		if f.Status == "posted" {
			out = append(out, f)
		}
	}
	return out
}
