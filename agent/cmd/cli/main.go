// The Habodek CLI: a dry-run review driven by the brain, and mirror pre-warming.
package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/joho/godotenv"
	"github.com/spf13/cobra"
	"golang.org/x/sync/semaphore"

	"habodek/internal/app"
	"habodek/internal/config"
	"habodek/internal/core/brain"
	"habodek/internal/orchestrator"
	"habodek/internal/pkg/git"
	"habodek/internal/pkg/gitlab"
	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/logger"
	"habodek/internal/pkg/memory"
	"habodek/internal/pkg/sandbox"
)

func main() {
	_ = godotenv.Load()
	root := &cobra.Command{Use: "habodek", Short: "Habodek control plane CLI"}
	root.AddCommand(reviewCmd(), mapCmd(), metricsCmd(), cloneAllCmd())
	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}

// mapCmd runs the cartographer on a project's default branch now and commits the
// repo card and facts to the memory repository (HABODEK_MEMORY_DIR). Use it to map
// the consumers of a library before its first review.
func mapCmd() *cobra.Command {
	var projectID int64
	var branch string
	cmd := &cobra.Command{
		Use:   "map",
		Short: "Map a project into memory now (the cartographer; writes HABODEK_MEMORY_DIR)",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := config.Load()
			if err != nil {
				return fmt.Errorf("config: %w", err)
			}
			logger.Setup(cfg.Log.Level, "text")
			if cfg.Memory.Dir == "" {
				return fmt.Errorf("HABODEK_MEMORY_DIR is none: nowhere to write the card")
			}
			ctx := cmd.Context()
			gl := gitlab.NewClient(cfg.GitLab.BaseURL, cfg.GitLab.Token)
			project, err := gl.GetProject(ctx, projectID)
			if err != nil {
				return fmt.Errorf("fetch project: %w", err)
			}
			if branch == "" {
				branch = project.DefaultBranch
			}
			if branch == "" {
				return fmt.Errorf("project %d has no default branch; pass --branch", projectID)
			}
			proxy := llmproxy.New(llmproxy.Config{UpstreamURL: cfg.Brain.GatewayURL,
				GatewayKey: cfg.Brain.GatewayKey, RPM: cfg.Brain.GatewayRPM})
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				return err
			}
			mux := http.NewServeMux()
			mux.Handle(llmproxy.Path, proxy)
			go func() { _ = http.Serve(listener, mux) }()
			defer listener.Close()

			jobID := fmt.Sprintf("cli-map-%d-%d", projectID, time.Now().Unix())
			gm := git.NewManager(cfg.Git.ReposDir, cfg.GitLab.BaseURL, cfg.GitLab.Token)
			workspaces := &brain.Workspaces{Git: gm, WorkDir: cfg.Brain.WorkDir}
			prepared, err := workspaces.PrepareBranch(ctx, jobID, projectID, project.PathWithNS, branch)
			if err != nil {
				return err
			}
			defer func() { prepared.Cleanup() }() // AddConsumers extends Cleanup
			runner := brain.NewRunner(brain.RunnerConfig{Command: cfg.Brain.Command,
				WorkDir: cfg.Brain.WorkDir, TraceDir: cfg.Brain.TraceDir,
				ProxyURL:         "http://" + listener.Addr().String() + "/internal/llm/v1",
				Timeout:          time.Duration(cfg.Brain.TimeoutMinutes) * time.Minute,
				InternalPackages: cfg.Brain.InternalPackages, PipIndexURL: cfg.Brain.PipIndexURL}, proxy)
			mem := memory.New(cfg.Memory.Dir)
			if err := os.MkdirAll(cfg.Memory.Dir, 0o755); err != nil {
				return err
			}
			dir, commit := mem.Dir, mem.Head(ctx)
			job := brain.Job{Kind: "map", JobID: jobID, Priority: "background",
				MR:        brain.MergeRequest{Project: project.PathWithNS, ProjectID: projectID, TargetBranch: branch},
				Revisions: prepared.Revisions, Workspace: prepared.Workspace, MemoryPath: &dir}
			if commit != "" {
				job.MemoryCommit = &commit
			}
			fmt.Printf("Mapping %s at %s on %s (trace: %s)\n", project.PathWithNS,
				shortSHA(prepared.Revisions.HeadSHA), branch, runner.TraceDir(jobID))
			result, err := runner.Run(ctx, job)
			if err != nil {
				return err
			}
			newCommit, err := mem.Write(func() (string, error) { return runner.ApplyMemory(ctx, mem.Dir, jobID) })
			if err != nil {
				return err
			}
			fmt.Printf("%s; memory commit %s (%d requests)\n", result.Summary, shortSHA(newCommit), result.Requests)
			return nil
		},
	}
	cmd.Flags().Int64Var(&projectID, "project", 0, "GitLab project ID")
	cmd.Flags().StringVar(&branch, "branch", "", "branch to map (default: the project's default branch)")
	_ = cmd.MarkFlagRequired("project")
	return cmd
}

// metricsCmd prints a project's acceptance per category and the bars it raises.
func metricsCmd() *cobra.Command {
	var projectID int64
	cmd := &cobra.Command{
		Use:   "metrics",
		Short: "Acceptance of posted findings per category, and the bars it raises",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := config.Load()
			if err != nil {
				return fmt.Errorf("config: %w", err)
			}
			driver, dsn := cfg.StoreDSN()
			store, err := orchestrator.Open(driver, dsn)
			if err != nil {
				return err
			}
			defer store.Close()
			bars := orchestrator.BarConfig{Disabled: cfg.Bars.Disabled, Window: cfg.Bars.Window,
				MinOutcomes: cfg.Bars.MinOutcomes, MajorBelow: cfg.Bars.MajorBelow,
				CriticalBelow: cfg.Bars.CriticalBelow}
			stats, err := store.Acceptance(cmd.Context(), projectID, bars)
			if err != nil {
				return err
			}
			raised := map[string]string{}
			for _, b := range orchestrator.Bars(stats, bars) {
				raised[b.Category] = b.MinSeverity + "+"
			}
			fmt.Printf("%-16s %6s %5s %8s %8s %7s %10s  %s\n", "category", "posted", "open",
				"accepted", "withdrawn", "neutral", "acceptance", "bar")
			for _, a := range stats {
				rate := "-"
				if a.Outcomes() > 0 {
					rate = fmt.Sprintf("%.0f%%", a.Rate*100)
				}
				fmt.Printf("%-16s %6d %5d %8d %8d %7d %10s  %s\n", a.Category, a.Posted, a.Open,
					a.Accepted, a.Rejected, a.Neutral, rate, raised[a.Category])
			}
			return nil
		},
	}
	cmd.Flags().Int64Var(&projectID, "project", 0, "GitLab project ID")
	_ = cmd.MarkFlagRequired("project")
	return cmd
}

// reviewCmd reviews one MR with the brain and prints the comments it would post.
// Nothing is written to GitLab. It needs the gateway settings, GitLab read access
// and habodek-brain on PATH (or HABODEK_BRAIN_CMD).
func reviewCmd() *cobra.Command {
	var projectID, mrIID int64
	cmd := &cobra.Command{
		Use:   "review",
		Short: "Dry run: review a merge request with the brain and print the result (posts nothing)",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := config.Load()
			if err != nil {
				return fmt.Errorf("config: %w", err)
			}
			logger.Setup(cfg.Log.Level, "text")
			ctx := context.Background()
			gl := gitlab.NewClient(cfg.GitLab.BaseURL, cfg.GitLab.Token)
			project, err := gl.GetProject(ctx, projectID)
			if err != nil {
				return fmt.Errorf("fetch project: %w", err)
			}
			mr, err := gl.GetMR(ctx, projectID, mrIID)
			if err != nil {
				return fmt.Errorf("fetch MR: %w", err)
			}
			// An in-process proxy on a loopback port: the brain still never sees the key.
			proxy := llmproxy.New(llmproxy.Config{UpstreamURL: cfg.Brain.GatewayURL,
				GatewayKey: cfg.Brain.GatewayKey, RPM: cfg.Brain.GatewayRPM})
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				return err
			}
			mux := http.NewServeMux()
			mux.Handle(llmproxy.Path, proxy)
			sb, err := app.NewSandbox(cfg)
			if err != nil {
				return err
			}
			if sb != nil {
				mux.Handle(sandbox.Path, sb)
			}
			go func() { _ = http.Serve(listener, mux) }()
			defer listener.Close()

			jobID := fmt.Sprintf("cli-%d-%d-%d", projectID, mrIID, time.Now().Unix())
			gm := git.NewManager(cfg.Git.ReposDir, cfg.GitLab.BaseURL, cfg.GitLab.Token)
			workspaces := &brain.Workspaces{Git: gm, WorkDir: cfg.Brain.WorkDir}
			prepared, err := workspaces.Prepare(ctx, jobID, projectID, project.PathWithNS, mr, "")
			if err != nil {
				return err
			}
			defer func() { prepared.Cleanup() }() // AddConsumers extends Cleanup
			runner := brain.NewRunner(brain.RunnerConfig{Command: cfg.Brain.Command,
				WorkDir: cfg.Brain.WorkDir, TraceDir: cfg.Brain.TraceDir,
				ProxyURL:         "http://" + listener.Addr().String() + "/internal/llm/v1",
				Timeout:          time.Duration(cfg.Brain.TimeoutMinutes) * time.Minute,
				InternalPackages: cfg.Brain.InternalPackages, PipIndexURL: cfg.Brain.PipIndexURL,
				SandboxURL: "http://" + listener.Addr().String() + "/internal/sandbox/v1"}, proxy)
			if sb != nil {
				runner.WithSandbox(sb)
			}
			var memPath, memCommit *string
			if cfg.Memory.Dir != "" { // read-only here: a dry run writes no memory
				mem := memory.New(cfg.Memory.Dir)
				dir, commit := mem.Dir, mem.Head(ctx)
				memPath = &dir
				if commit != "" {
					memCommit = &commit
				}
				var consumers []brain.Consumer
				for _, f := range mem.Consumers(project.PathWithNS) {
					if f.GitLabID != 0 && f.DefaultBranch != "" && len(consumers) < cfg.Memory.MaxConsumers {
						consumers = append(consumers, brain.Consumer{ProjectID: f.GitLabID, Path: f.Project,
							Branch: f.DefaultBranch})
					}
				}
				for _, cerr := range workspaces.AddConsumers(ctx, prepared, jobID, consumers) {
					fmt.Println("consumer skipped:", cerr)
				}
			}
			fmt.Printf("Reviewing %s!%d at %s (trace: %s)\n", project.PathWithNS, mrIID,
				shortSHA(mr.HeadSHA), runner.TraceDir(jobID))
			result, err := runner.Run(ctx, brain.Job{Kind: "review", JobID: jobID, Priority: "review",
				MR: brain.MergeRequest{Project: project.PathWithNS, IID: mr.IID, Title: mr.Title,
					Description: mr.Description, Author: mr.AuthorUsername, Labels: mr.Labels,
					SourceBranch: mr.SourceBranch, TargetBranch: mr.TargetBranch, WebURL: mr.WebURL},
				Revisions: prepared.Revisions, Workspace: prepared.Workspace,
				MaxComments: cfg.Brain.MaxComments, MemoryPath: memPath, MemoryCommit: memCommit})
			if err != nil {
				return err
			}
			for i, c := range result.Comments {
				fmt.Printf("\n── Comment %d on %s:%d (%s side) ──\n%s\n", i+1, c.Path, c.Line, c.Side, c.Body)
			}
			fmt.Printf("\n── Summary (%s, %d requests) ──\n%s\n", result.Status, result.Requests,
				strings.TrimSpace(result.Summary))
			return nil
		},
	}
	cmd.Flags().Int64Var(&projectID, "project", 0, "GitLab project ID")
	cmd.Flags().Int64Var(&mrIID, "mr", 0, "merge request IID")
	_ = cmd.MarkFlagRequired("project")
	_ = cmd.MarkFlagRequired("mr")
	return cmd
}

func shortSHA(sha string) string {
	if len(sha) > 8 {
		return sha[:8]
	}
	return sha
}

func cloneAllCmd() *cobra.Command {
	var concurrency int
	var cloneTimeout time.Duration
	var outputDir string
	var skipExisting bool
	var minAccessLevel int

	cmd := &cobra.Command{
		Use:   "clone-all",
		Short: "Clone all accessible GitLab repositories into the repos directory",
		Long: `Fetches every GitLab project the token can access and clones it locally.
Repositories that already exist are updated with git fetch instead of re-cloned.
Useful to pre-warm the local repo cache before starting the review server.

Access levels: 10=guest, 20=reporter, 30=developer, 40=maintainer, 50=owner`,
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := config.Load()
			if err != nil {
				return fmt.Errorf("config: %w", err)
			}
			logger.Setup(cfg.Log.Level, cfg.Log.Format)

			reposDir := cfg.Git.ReposDir
			if outputDir != "" {
				reposDir = outputDir
			}
			if err := os.MkdirAll(reposDir, 0o755); err != nil {
				return fmt.Errorf("create repos dir: %w", err)
			}

			ctx := context.Background()
			gitlabClient := gitlab.NewClient(cfg.GitLab.BaseURL, cfg.GitLab.Token)

			fmt.Printf("Fetching project list from %s", cfg.GitLab.BaseURL)
			if minAccessLevel > 0 {
				fmt.Printf(" (min access level %d)", minAccessLevel)
			}
			fmt.Println("...")

			projects, err := gitlabClient.ListAllProjects(ctx, minAccessLevel)
			if err != nil {
				return fmt.Errorf("list projects: %w", err)
			}
			fmt.Printf("Found %d projects. Cloning with concurrency=%d, per-repo timeout=%s\n\n",
				len(projects), concurrency, cloneTimeout)

			gitManager := git.NewManager(reposDir, cfg.GitLab.BaseURL, cfg.GitLab.Token)

			sem := semaphore.NewWeighted(int64(concurrency))
			var wg sync.WaitGroup

			var (
				nCloned  atomic.Int64
				nFetched atomic.Int64
				nSkipped atomic.Int64
				nFailed  atomic.Int64
				failMu   sync.Mutex
				failures []string
			)

			for _, proj := range projects {
				wg.Add(1)
				go func(p gitlab.ProjectBasic) {
					defer wg.Done()

					if acquireErr := sem.Acquire(ctx, 1); acquireErr != nil {
						nFailed.Add(1)
						fmt.Printf("  [FAIL] %s: %v\n", p.PathWithNamespace, acquireErr)
						return
					}
					defer sem.Release(1)

					if skipExisting {
						gitDir := filepath.Join(gitManager.RepoPath(p.ID), ".git")
						if _, statErr := os.Stat(gitDir); statErr == nil {
							nSkipped.Add(1)
							fmt.Printf("  [SKIP] %s\n", p.PathWithNamespace)
							return
						}
					}

					repoCtx, cancel := context.WithTimeout(ctx, cloneTimeout)
					defer cancel()

					cloned, opErr := gitManager.CloneOrFetch(repoCtx, p.ID, p.HTTPURLToRepo)
					if opErr != nil {
						nFailed.Add(1)
						msg := fmt.Sprintf("%s: %v", p.PathWithNamespace, opErr)
						failMu.Lock()
						failures = append(failures, msg)
						failMu.Unlock()
						fmt.Printf("  [FAIL] %s\n", msg)
						return
					}
					if cloned {
						nCloned.Add(1)
						fmt.Printf("  [NEW]  %s\n", p.PathWithNamespace)
					} else {
						nFetched.Add(1)
						fmt.Printf("  [OK]   %s\n", p.PathWithNamespace)
					}
				}(proj)
			}

			wg.Wait()

			fmt.Println()
			fmt.Println(strings.Repeat("─", 60))
			fmt.Printf("Done. cloned=%d  fetched=%d  skipped=%d  failed=%d\n",
				nCloned.Load(), nFetched.Load(), nSkipped.Load(), nFailed.Load())

			if len(failures) > 0 {
				fmt.Printf("\nFailed repos (%d):\n", len(failures))
				for _, f := range failures {
					fmt.Printf("  • %s\n", f)
				}
				return fmt.Errorf("%d repo(s) failed", len(failures))
			}
			return nil
		},
	}

	cmd.Flags().IntVar(&concurrency, "concurrency", 3, "Number of parallel clone/fetch operations")
	cmd.Flags().DurationVar(&cloneTimeout, "clone-timeout", 30*time.Minute, "Per-repo operation timeout (increase for very large repos)")
	cmd.Flags().StringVar(&outputDir, "output-dir", "", "Override repos directory (default: GIT_REPOS_DIR from config)")
	cmd.Flags().BoolVar(&skipExisting, "skip-existing", false, "Skip repos that already have a local clone (do not fetch updates)")
	cmd.Flags().IntVar(&minAccessLevel, "min-access-level", 0, "Minimum access level filter (0=all, 20=reporter, 30=developer, 40=maintainer, 50=owner)")
	cmd.SilenceUsage = true

	return cmd
}
