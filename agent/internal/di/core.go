package di

import (
	"github.com/samber/do/v2"

	"habodek/internal/config"
	"habodek/internal/core/agents/replier"
	"habodek/internal/core/agents/reviewer"
	"habodek/internal/core/feedback"
	"habodek/internal/core/reply"
	"habodek/internal/core/review"
	"habodek/internal/domain"
	"habodek/internal/pkg/git"
	"habodek/internal/pkg/llm"
)

var CorePackage = do.Package(
	do.Lazy(provideReviewAgent),
	do.Lazy(provideReplyAgent),
	do.Lazy(provideContextGatherer),
	do.Lazy(provideReviewPipeline),
	do.Lazy(provideReplyPipeline),
	do.Lazy(provideFeedbackConsolidator),
)

func provideReviewAgent(_ do.Injector) (*reviewer.Agent, error) {
	return reviewer.NewAgent(), nil
}

func provideReplyAgent(_ do.Injector) (*replier.Agent, error) {
	return replier.NewAgent(), nil
}

func provideContextGatherer(i do.Injector) (*review.ContextGatherer, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return review.NewContextGatherer(
		do.MustInvoke[domain.GitLabClient](i),
		do.MustInvoke[domain.RepositorySettingsStore](i),
		do.MustInvoke[domain.FeedbackStore](i),
		cfg.GitLab.BotUserID,
	), nil
}

func provideReviewPipeline(i do.Injector) (*review.Pipeline, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return review.NewPipeline(review.PipelineDeps{
		Config:        *cfg,
		JobStore:      do.MustInvoke[domain.ReviewJobStore](i),
		RepoSettings:  do.MustInvoke[domain.RepositorySettingsStore](i),
		RecordStore:   do.MustInvoke[domain.ReviewRecordStore](i),
		FeedbackStore: do.MustInvoke[domain.FeedbackStore](i),
		GitLabClient:  do.MustInvoke[domain.GitLabClient](i),
		GitManager:    do.MustInvoke[*git.Manager](i),
		Gatherer:      do.MustInvoke[*review.ContextGatherer](i),
		Agent:         do.MustInvoke[*reviewer.Agent](i),
	}), nil
}

func provideReplyPipeline(i do.Injector) (*reply.Pipeline, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return reply.NewPipeline(reply.PipelineDeps{
		Config:        *cfg,
		ReplyJobStore: do.MustInvoke[domain.ReplyJobStore](i),
		RepoSettings:  do.MustInvoke[domain.RepositorySettingsStore](i),
		FeedbackStore: do.MustInvoke[domain.FeedbackStore](i),
		GitLabClient:  do.MustInvoke[domain.GitLabClient](i),
		ReplyAgent:    do.MustInvoke[*replier.Agent](i),
		RepoManager:   do.MustInvoke[*git.Manager](i),
	}), nil
}

func provideFeedbackConsolidator(i do.Injector) (*feedback.Consolidator, error) {
	cfg := do.MustInvoke[*config.Config](i)
	llmClient, err := llm.NewBalancedClientFromConfig(cfg.LLM, nil)
	if err != nil {
		return nil, err
	}
	return feedback.NewConsolidator(
		do.MustInvoke[domain.FeedbackStore](i),
		do.MustInvoke[domain.RepositorySettingsStore](i),
		llmClient,
		cfg.Cron.FeedbackConsolidateMinCount,
		cfg.Cron.FeedbackConsolidateMinAgeDays,
		cfg.Cron.FeedbackCustomPromptMaxWords,
	), nil
}
