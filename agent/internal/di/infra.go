package di

import (
	"github.com/samber/do/v2"

	"habodek/internal/config"
	"habodek/internal/domain"
	"habodek/internal/pkg/git"
	"habodek/internal/pkg/gitlab"
	"habodek/internal/pkg/queue"
	"habodek/internal/pkg/store"
)

var InfraPackage = do.Package(
	do.Lazy(provideStores),
	do.Lazy(provideRepositorySettingsStore),
	do.Lazy(provideReviewJobStore),
	do.Lazy(provideReplyJobStore),
	do.Lazy(provideFeedbackStore),
	do.Lazy(provideReviewRecordStore),
	do.Lazy(provideGitLabClient),
	do.Lazy(provideGitManager),
	do.Lazy(provideJobQueue),
)

func provideStores(i do.Injector) (*store.Stores, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return store.New(cfg.Store)
}

// The individual store interfaces are separate lazy providers, each pulling the
// one *store.Stores, rather than being registered as a side effect of building
// it. Registering them inside provideStores made resolution order-dependent: it
// worked from cmd/cli, which invokes *store.Stores first, and panicked from
// cmd/server, which reaches domain.ReviewJobStore through the worker pool before
// anything has asked for *store.Stores.

func provideRepositorySettingsStore(i do.Injector) (domain.RepositorySettingsStore, error) {
	return do.MustInvoke[*store.Stores](i).RepoSettings, nil
}

func provideReviewJobStore(i do.Injector) (domain.ReviewJobStore, error) {
	return do.MustInvoke[*store.Stores](i).ReviewJobs, nil
}

func provideReplyJobStore(i do.Injector) (domain.ReplyJobStore, error) {
	return do.MustInvoke[*store.Stores](i).ReplyJobs, nil
}

func provideFeedbackStore(i do.Injector) (domain.FeedbackStore, error) {
	return do.MustInvoke[*store.Stores](i).Feedbacks, nil
}

func provideReviewRecordStore(i do.Injector) (domain.ReviewRecordStore, error) {
	return do.MustInvoke[*store.Stores](i).ReviewRecords, nil
}

func provideGitLabClient(i do.Injector) (domain.GitLabClient, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return gitlab.NewClient(cfg.GitLab.BaseURL, cfg.GitLab.Token), nil
}

func provideGitManager(i do.Injector) (*git.Manager, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return git.NewManager(cfg.Git.ReposDir, cfg.GitLab.BaseURL, cfg.GitLab.Token), nil
}

func provideJobQueue(_ do.Injector) (*queue.Queue, error) {
	return queue.New(), nil
}
