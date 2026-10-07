package main

import (
	"context"
	"errors"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/admin"
	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/diagnostics"
	"github.com/wongyiuming/FrontierCloud/internal/httpapi"
	"github.com/wongyiuming/FrontierCloud/internal/karaoke"
	"github.com/wongyiuming/FrontierCloud/internal/media"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/node"
	"github.com/wongyiuming/FrontierCloud/internal/observation"
	"github.com/wongyiuming/FrontierCloud/internal/recording"
	"github.com/wongyiuming/FrontierCloud/internal/release"
	"github.com/wongyiuming/FrontierCloud/internal/security"
	"github.com/wongyiuming/FrontierCloud/internal/sitecontrol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

// runtimeHTTP is the single production registration path, also exercised by
// route and behavior acceptance. It does not alter startup admission policy.
type runtimeHTTP struct {
	Database   store.Store
	Identity   *node.Identity
	Media      *media.Service
	Control    *node.Service
	Recordings *recording.Storage
	Manager    *recording.Manager
	Security   *security.Service
	Redis      *redis.Client
	Resolver   *network.Resolver
	Agent      release.Agent
}

func newRuntimeHTTP(settings config.Config, services runtimeHTTP) (*gin.Engine, func() error, error) {
	database, identity := services.Database, services.Identity
	mediaService, controlService := services.Media, services.Control
	recordingStorage, recordingManager := services.Recordings, services.Manager
	securityService, redisClient := services.Security, services.Redis
	resolver := services.Resolver
	if database == nil || identity == nil || mediaService == nil || controlService == nil || recordingStorage == nil || recordingManager == nil || securityService == nil || redisClient == nil || resolver == nil {
		return nil, nil, errors.New("incomplete native HTTP services")
	}
	var closers []func() error
	closeHTTP := func() error {
		var result error
		for i := len(closers) - 1; i >= 0; i-- {
			result = errors.Join(result, closers[i]())
		}
		closers = nil
		return result
	}
	ready := false
	defer func() {
		if !ready {
			closeHTTP()
		}
	}()
	handler := httpapi.NewWithResolver(func(ctx context.Context) error {
		if err := database.Ping(ctx); err != nil {
			return err
		}
		if err := mediaService.Ready(ctx); err != nil {
			return err
		}
		if err := recordingStorage.Ready(ctx); err != nil {
			return err
		}
		return securityService.Ready(ctx)
	}, func(ctx context.Context) error {
		return redisClient.Ping(ctx).Err()
	}, resolver, httpapi.SecurityMiddleware(securityService, resolver))
	public, err := httpapi.RegisterPublic(handler, settings, mediaService)
	if err != nil {
		return nil, nil, err
	}
	closers = append(closers, public.Close)
	adminService, err := admin.New(settings, admin.NewRedisCache(redisClient), database.Admin())
	if err != nil {
		return nil, nil, err
	}
	adminHTTP, err := httpapi.RegisterAdmin(handler, settings, adminService, public, identity)
	if err != nil {
		return nil, nil, err
	}
	httpapi.RegisterAdminNodes(handler, adminHTTP, controlService)
	httpapi.RegisterOperational(handler, adminHTTP)
	httpapi.RegisterPlaybackDiagnostics(handler, settings, resolver, controlService, adminHTTP, diagnostics.New())
	policy := release.Policy{Branch: settings.ReleaseBranch, Source: settings.ReleaseSourceBranch}
	verifier, err := release.NewVerifier(policy, settings.GitHubAPIToken)
	if err != nil {
		return nil, nil, err
	}
	agent := services.Agent
	if agent == nil {
		agent = release.SocketAgent{}
	}
	siteService, err := sitecontrol.Open(settings.DataRoot, agent)
	if err != nil {
		return nil, nil, err
	}
	closers = append(closers, siteService.Close)
	httpapi.RegisterSiteAdmin(handler, adminHTTP, siteService)
	releases := &release.Coordinator{Agent: agent, Verifier: verifier, Nodes: database.Nodes(), Control: controlService, Policy: policy}
	if settings.ReleaseManifestPath != "" {
		releases.ManifestSource = release.FileManifestSource{Path: settings.ReleaseManifestPath}
		releases.ManifestEvidence = map[string]release.ArtifactEvidence{}
		for _, branch := range []string{"main", "gin_main"} {
			profile, _ := release.PolicyForBranch(branch)
			proof, e := release.NewVerifier(profile, settings.GitHubAPIToken)
			if e != nil {
				return nil, nil, e
			}
			releases.ManifestEvidence[branch] = proof
		}
	}
	httpapi.RegisterReleaseAdmin(handler, adminHTTP, releases)
	httpapi.RegisterNodeRelease(handler, settings, resolver, controlService, agent)
	httpapi.RegisterSecurityAdmin(handler, adminHTTP, securityService)
	accountsHTTP := httpapi.RegisterKaraokeAccounts(handler, karaoke.New(database.Karaoke(), database.Nodes(), karaoke.NewRedisCache(redisClient)), public, adminHTTP, resolver)
	httpapi.RegisterKaraokeRecordings(handler, accountsHTTP, recordingManager, recordingStorage)
	httpapi.RegisterKaraokeMedia(handler, public, resolver)
	httpapi.RegisterNodeRecordings(handler, settings, resolver, controlService, recordingStorage)
	httpapi.RegisterNodeIdentity(handler, settings, resolver, controlService)
	httpapi.RegisterNodeControl(handler, settings, resolver, controlService)
	httpapi.RegisterNodeBackups(handler, settings, resolver, controlService, database.Backups())
	httpapi.RegisterNodeMedia(handler, settings, resolver, controlService, mediaService)
	httpapi.RegisterNodeStorage(handler, settings, resolver, controlService, mediaService)
	httpapi.RegisterObservations(handler, adminHTTP, observation.New(database.Observations(), redisClient, settings.WebRTCCooldown), resolver)

	ready = true
	return handler, closeHTTP, nil
}
