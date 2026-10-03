package updater

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
	"github.com/wongyiuming/FrontierCloud/internal/sitecontrol"
)

type DockerExecutor struct {
	Source                            Source
	Socket, Project, ControlDirectory string
	Runtime                           string
	DataDirectory                     string
	ManifestVerifier                  release.ArtifactEvidence
}
type replacement struct {
	Target    string               `json:"target"`
	Old       string               `json:"old_sha"`
	Snapshots map[string]Container `json:"snapshots"`
	Helpers   []string             `json:"helpers"`
}
type handoff struct {
	Target   string    `json:"target"`
	Image    string    `json:"image"`
	Snapshot Container `json:"snapshot"`
	Helper   string    `json:"helper"`
}

func (x *DockerExecutor) private() (*privateStore, error) {
	return openPrivate(x.ControlDirectory, false)
}
func (x *DockerExecutor) engine(ctx context.Context) (*Engine, error) {
	if !dockerName.MatchString(x.Project) || !release.ValidSHA(x.Runtime) {
		return nil, ErrState
	}
	e, err := NewEngine(ctx, x.Socket)
	if err == nil {
		e.Project = x.Project
	}
	return e, err
}
func previousFor(request Request, before Status) string {
	if request.Mode == "rollback" {
		return ""
	}
	if request.Target != before.CurrentSHA {
		return before.CurrentSHA
	}
	return before.PreviousSHA
}
func (x *DockerExecutor) Execute(parent context.Context, request Request, before Status, progress func(Checkpoint) error) (Outcome, error) {
	ctx, cancel := context.WithTimeout(parent, 45*time.Minute)
	defer cancel()
	if request.Manifest != nil {
		if err := x.verifyManifest(ctx, request, before); err != nil {
			return Outcome{}, err
		}
	}
	if err := x.clearForceOpen(ctx); err != nil {
		return Outcome{}, err
	}
	if err := x.Source.Validate(ctx, request.Target, request.Mode); err != nil {
		return Outcome{}, err
	}
	engine, err := x.engine(ctx)
	if err != nil {
		return Outcome{}, err
	}
	defer engine.Close()
	private, err := x.private()
	if err != nil {
		return Outcome{}, err
	}
	defer private.Close()
	// A previous incomplete journal must be recovered, not overwritten by retry.
	var oldJournal replacement
	if e := private.read("replacement.json", 16<<20, &oldJournal); e == nil {
		return Outcome{}, errors.New("replacement recovery is incomplete")
	} else if !errors.Is(e, os.ErrNotExist) {
		return Outcome{}, e
	}
	var journal replacement
	journal.Target, journal.Old, journal.Snapshots = request.Target, before.CurrentSHA, map[string]Container{}
	for _, service := range []string{"web", "nginx", "updater", "secrets-init", "media-init"} {
		c, e := engine.Service(ctx, x.Project, service)
		if e != nil {
			return Outcome{}, e
		}
		component := service
		if service == "secrets-init" || service == "media-init" {
			component = "web"
		}
		revision := before.CurrentSHA
		if service == "updater" {
			revision = x.Runtime
		}
		if _, e = engine.Image(ctx, c.Image, revision, component); e != nil {
			return Outcome{}, e
		}
		journal.Snapshots[service] = c
	}
	previous := previousFor(request, before)
	web := journal.Snapshots["web"]
	if request.Target == before.CurrentSHA && (request.Manifest != nil || before.CurrentManifest != nil || before.PreviousManifest != nil) {
		for _, component := range []string{"web", "updater"} {
			c := journal.Snapshots[component]
			revision := before.CurrentSHA
			if component == "updater" {
				revision = x.Runtime
			}
			image, e := engine.Image(ctx, c.Image, revision, component)
			if e != nil || image.Config.Labels["frontiercloud.release-manifest-version"] != "1" {
				return Outcome{}, release.ErrManifest
			}
		}
	}
	if request.Target != before.CurrentSHA {
		if err = progress(Checkpoint{Phase: "building"}); err != nil {
			return Outcome{}, err
		}
		webImage, e := engine.Build(ctx, x.Source, request.Target, "web", "Dockerfile.gin")
		if e != nil {
			return Outcome{}, e
		}
		nginxImage, e := engine.Build(ctx, x.Source, request.Target, "nginx", "nginx/Dockerfile")
		if e != nil {
			return Outcome{}, e
		}
		if _, e = engine.Build(ctx, x.Source, request.Target, "updater", "updater/Dockerfile.gin"); e != nil {
			return Outcome{}, e
		}
		if request.Manifest != nil || before.CurrentManifest != nil || before.PreviousManifest != nil {
			for component, ref := range map[string]string{"web": webImage, "updater": "frontiercloud-updater:" + request.Target} {
				image, e := engine.Image(ctx, ref, request.Target, component)
				if e != nil || image.Config.Labels["frontiercloud.release-manifest-version"] != "1" {
					return Outcome{}, release.ErrManifest
				}
			}
		}
		if e = private.write("replacement.json", journal, 16<<20); e != nil {
			return Outcome{}, e
		}
		if e = progress(Checkpoint{Phase: "replacing"}); e != nil {
			return Outcome{}, e
		}
		localCommitted := false
		defer func() {
			if !localCommitted {
				// A bounded detached recovery context survives request cancellation.
				recovery, c := context.WithTimeout(context.Background(), 8*time.Minute)
				defer c()
				if x.restore(recovery, engine, private, journal) == nil {
					private.remove("replacement.json")
				}
			}
		}()
		if e = x.helper(ctx, engine, private, &journal, "enter", journal.Snapshots["web"], web.Image, []string{"maintenance", "enter", "--wait-seconds", "300"}); e != nil {
			return Outcome{}, e
		}
		if e = engine.Stop(ctx, web.ID); e != nil {
			return Outcome{}, e
		}
		if e = x.helper(ctx, engine, private, &journal, "prepare", web, webImage, []string{"prepare-release", "--confirm-generation", "2"}); e != nil {
			return Outcome{}, e
		}
		if e = x.helper(ctx, engine, private, &journal, "resume", web, webImage, []string{"maintenance", "resume", "--wait-seconds", "300"}); e != nil {
			return Outcome{}, e
		}
		for _, service := range []string{"secrets-init", "media-init"} {
			value, e := engine.Replace(ctx, journal.Snapshots[service], webImage, request.Target)
			if e != nil {
				return Outcome{}, e
			}
			if e = engine.Wait(ctx, value.ID); e != nil {
				return Outcome{}, e
			}
		}
		web, e = engine.Replace(ctx, journal.Snapshots["web"], webImage, request.Target)
		if e != nil {
			return Outcome{}, e
		}
		health, c := context.WithTimeout(ctx, 4*time.Minute)
		e = engine.Healthy(health, web.ID)
		c()
		if e != nil {
			return Outcome{}, e
		}
		nginx, e := engine.Replace(ctx, journal.Snapshots["nginx"], nginxImage, request.Target)
		if e != nil {
			return Outcome{}, e
		}
		if e = engine.NginxReady(ctx, nginx.ID); e != nil {
			return Outcome{}, e
		}
		// Commit local generation before distribution. A later remote outage
		// cannot roll back already healthy local services or their business data.
		if e = progress(Checkpoint{Current: request.Target, Previous: previous, SetPrevious: true, Phase: "local-committed"}); e != nil {
			return Outcome{}, e
		}
		localCommitted = true
		if e = private.remove("replacement.json"); e != nil {
			return Outcome{}, e
		}
	}
	health, stop := context.WithTimeout(ctx, 4*time.Minute)
	err = engine.Healthy(health, web.ID)
	stop()
	if err != nil {
		return Outcome{}, err
	}
	currentNginx, err := engine.Service(ctx, x.Project, "nginx")
	if err != nil {
		return Outcome{}, err
	}
	if err = engine.NginxReady(ctx, currentNginx.ID); err != nil {
		return Outcome{}, err
	}
	if request.Target == before.CurrentSHA && request.Manifest != nil {
		// A joint release can change only another profile. Publish local whole
		// release history durably even when no native containers are replaced.
		if err = progress(Checkpoint{Current: request.Target, Previous: previous, SetPrevious: true, Phase: "local-committed"}); err != nil {
			return Outcome{}, err
		}
	}
	if request.Hold {
		if err = progress(Checkpoint{State: "distributing", Phase: "distributing"}); err != nil {
			return Outcome{}, err
		}
		command := []string{"/app/frontiercloud", "cluster-release", request.Target, request.Mode}
		if request.Manifest != nil {
			wire, e := request.Manifest.Wire()
			if e != nil {
				return Outcome{}, e
			}
			command = []string{"/app/frontiercloud", "cluster-release-manifest", protocol.Encode(wire), request.Mode}
		}
		if err = engine.Exec(ctx, web.ID, command); err != nil {
			return Outcome{}, err
		}
	}
	engine.Cleanup(ctx, request.Target, previous)
	if x.Runtime == request.Target {
		return Outcome{Current: request.Target, Previous: previous}, nil
	}
	updaterImage := "frontiercloud-updater:" + request.Target
	if _, err = engine.Image(ctx, updaterImage, request.Target, "updater"); errors.Is(err, ErrNotFound) {
		updaterImage, err = engine.Build(ctx, x.Source, request.Target, "updater", "updater/Dockerfile.gin")
	}
	if err != nil {
		return Outcome{}, err
	}
	value := handoff{Target: request.Target, Image: updaterImage, Snapshot: journal.Snapshots["updater"], Helper: "fc-handoff-" + request.Target + "-" + journal.Snapshots["updater"].ID[:12]}
	if err = private.write("handoff.json", value, 16<<20); err != nil {
		return Outcome{}, err
	}
	if err = progress(Checkpoint{State: "restarting", Phase: "updater-restart", Current: request.Target, Previous: previous, SetPrevious: true}); err != nil {
		return Outcome{}, err
	}
	if _, err = engine.Helper(ctx, value.Snapshot, value.Image, value.Helper, []string{"handoff"}, true); err != nil {
		return Outcome{}, err
	}
	return Outcome{Current: request.Target, Previous: previous, Handoff: true}, nil
}

func (x *DockerExecutor) verifyManifest(ctx context.Context, request Request, before Status) error {
	policy, err := release.PolicyForBranch(x.Source.Branch)
	if err != nil || before.ReleaseBranch != policy.Branch || request.Manifest == nil {
		return release.ErrManifest
	}
	artifact, err := request.Manifest.Select(policy)
	if err != nil || artifact.CommitSHA != request.Target {
		return release.ErrManifest
	}
	verifier := x.ManifestVerifier
	if verifier == nil {
		verifier, err = release.NewVerifier(policy, os.Getenv("GITHUB_API_TOKEN"))
		if err != nil {
			return err
		}
	}
	proof, err := verifier.Artifact(ctx, artifact.CommitSHA)
	if err != nil {
		return err
	}
	return request.Manifest.CheckEvidence(policy, proof)
}
func (x *DockerExecutor) helper(ctx context.Context, e *Engine, s *privateStore, j *replacement, phase string, snap Container, image string, command []string) error {
	name := "fc-release-" + phase + "-" + j.Target + "-" + snap.ID[:12]
	j.Helpers = append(j.Helpers, name)
	if err := s.write("replacement.json", j, 16<<20); err != nil {
		return err
	}
	_, err := e.Helper(ctx, snap, image, name, command, false)
	return err
}
func (x *DockerExecutor) retireHelpers(ctx context.Context, e *Engine, j replacement) error {
	for _, name := range j.Helpers {
		if !dockerName.MatchString(name) || !strings.HasPrefix(name, "fc-release-") {
			return ErrState
		}
		c, err := e.Inspect(ctx, name)
		if errors.Is(err, ErrNotFound) {
			continue
		}
		if err != nil {
			return err
		}
		if c.label("frontiercloud.helper") != name || c.label("frontiercloud.project") != x.Project {
			return ErrState
		}
		if err = e.Stop(ctx, c.ID); err != nil {
			return err
		}
		if err = e.Remove(ctx, c.ID); err != nil {
			return err
		}
	}
	return nil
}
func (x *DockerExecutor) restore(ctx context.Context, e *Engine, s *privateStore, j replacement) error {
	if !release.ValidSHA(j.Target) || !release.ValidSHA(j.Old) || len(j.Snapshots) != 5 {
		return ErrState
	}
	for _, service := range []string{"web", "nginx", "updater", "secrets-init", "media-init"} {
		c := j.Snapshots[service]
		if !c.valid() || c.label("com.docker.compose.project") != x.Project || c.label("com.docker.compose.service") != service {
			return ErrState
		}
		component := service
		if service == "secrets-init" || service == "media-init" {
			component = "web"
		}
		revision := j.Old
		if service == "updater" {
			revision = x.Runtime
		}
		if _, err := e.Image(ctx, c.Image, revision, component); err != nil {
			return err
		}
	}
	if err := x.retireHelpers(ctx, e, j); err != nil {
		return err
	}
	web := j.Snapshots["web"]
	// Drain a partially started replacement with its native helper. No reverse
	// schema migration is attempted; only exact shared generation 2 is admitted.
	if err := x.helper(ctx, e, s, &j, "recovery-enter", web, web.Image, []string{"maintenance", "enter", "--wait-seconds", "300"}); err != nil {
		return err
	}
	if current, err := e.Inspect(ctx, strings.TrimPrefix(web.Name, "/")); err == nil {
		if current.ID != web.ID && current.label("frontiercloud.release-operation") != j.Target {
			return ErrState
		}
		if err = e.Stop(ctx, current.ID); err != nil {
			return err
		}
	} else if !errors.Is(err, ErrNotFound) {
		return err
	}
	if err := x.helper(ctx, e, s, &j, "recovery-resume", web, web.Image, []string{"maintenance", "resume", "--wait-seconds", "300"}); err != nil {
		return err
	}
	for _, service := range []string{"secrets-init", "media-init", "web", "nginx"} {
		c, err := e.Replace(ctx, j.Snapshots[service], j.Snapshots[service].Image, j.Target)
		if err != nil {
			return err
		}
		if service == "web" {
			health, cancel := context.WithTimeout(ctx, 4*time.Minute)
			err = e.Healthy(health, c.ID)
			cancel()
		} else if service == "nginx" {
			err = e.NginxReady(ctx, c.ID)
		} else {
			err = e.Wait(ctx, c.ID)
		}
		if err != nil {
			return err
		}
	}
	return nil
}
func (x *DockerExecutor) Recover(ctx context.Context, status Status) error {
	e, err := x.engine(ctx)
	if err != nil {
		return err
	}
	defer e.Close()
	s, err := x.private()
	if err != nil {
		return err
	}
	defer s.Close()
	var journal replacement
	journalErr := s.read("replacement.json", 16<<20, &journal)
	var pending handoff
	handoffErr := s.read("handoff.json", 16<<20, &pending)
	if handoffErr != nil && !errors.Is(handoffErr, os.ErrNotExist) {
		return handoffErr
	}
	if handoffErr == nil && !x.validHandoff(pending, status.TargetSHA) {
		return ErrState
	}
	if journalErr == nil || handoffErr == nil || release.Busy(status.State) || status.State == "success" {
		if err = x.clearForceOpen(ctx); err != nil {
			return err
		}
	}
	if err = journalErr; err == nil {
		if journal.Target != status.TargetSHA || (journal.Old != status.CurrentSHA && status.CurrentSHA != journal.Target) {
			return ErrState
		}
		if status.CurrentSHA != journal.Target {
			if err = x.restore(ctx, e, s, journal); err != nil {
				return err
			}
		}
		if err = s.remove("replacement.json"); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if status.State == "failed" && handoffErr == nil {
		if pending.Target != status.CurrentSHA || status.RuntimeSHA != x.Runtime {
			return ErrState
		}
		current, err := e.Service(ctx, x.Project, "updater")
		if err != nil {
			return err
		}
		if current.State.Status != "running" {
			return ErrState
		}
		image, imageErr := e.Image(ctx, current.Image, x.Runtime, "updater")
		if imageErr != nil {
			return imageErr
		}
		if manifestState(status) && image.Config.Labels["frontiercloud.release-manifest-version"] != "1" {
			return release.ErrManifest
		}
		if err = x.retireHandoffHelper(ctx, e, pending); err != nil {
			return err
		}
		if err = s.remove("handoff.json"); err != nil {
			return err
		}
	}
	if status.State == "success" || status.State == "restarting" {
		if x.Runtime != status.CurrentSHA || (status.State == "restarting" && x.Runtime != status.TargetSHA) {
			return ErrState
		}
		for _, service := range []string{"web", "nginx", "updater"} {
			c, err := e.Service(ctx, x.Project, service)
			if err != nil {
				return err
			}
			image, imageErr := e.Image(ctx, c.Image, status.CurrentSHA, service)
			if imageErr != nil {
				return imageErr
			}
			if manifestState(status) && service != "nginx" && image.Config.Labels["frontiercloud.release-manifest-version"] != "1" {
				return release.ErrManifest
			}
			if service == "web" {
				if c.State.Status != "running" || c.State.Health.Status != "healthy" {
					return ErrState
				}
			}
			if service == "nginx" {
				if err = e.NginxReady(ctx, c.ID); err != nil {
					return err
				}
			}
			if service == "updater" && c.State.Status != "running" {
				return ErrState
			}
		}
		var h handoff
		if err = s.read("handoff.json", 16<<20, &h); err == nil {
			if h.Target != status.CurrentSHA {
				return ErrState
			}
			if err = x.retireHandoffHelper(ctx, e, h); err != nil {
				return err
			}
			if err = s.remove("handoff.json"); err != nil {
				return err
			}
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

func manifestState(status Status) bool {
	return status.TargetManifest != nil || status.CurrentManifest != nil || status.PreviousManifest != nil
}

func (x *DockerExecutor) validHandoff(h handoff, target string) bool {
	return release.ValidSHA(target) && h.Target == target && h.Snapshot.valid() && h.Snapshot.label("com.docker.compose.project") == x.Project && h.Snapshot.label("com.docker.compose.service") == "updater" && h.Helper == "fc-handoff-"+h.Target+"-"+h.Snapshot.ID[:12]
}
func (x *DockerExecutor) retireHandoffHelper(ctx context.Context, e *Engine, h handoff) error {
	if !x.validHandoff(h, h.Target) {
		return ErrState
	}
	c, err := e.Inspect(ctx, h.Helper)
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if c.label("frontiercloud.helper") != h.Helper || c.label("frontiercloud.project") != x.Project {
		return ErrState
	}
	if err = e.Stop(ctx, c.ID); err != nil {
		return err
	}
	return e.Remove(ctx, c.ID)
}

func (x *DockerExecutor) clearForceOpen(ctx context.Context) error {
	info, err := os.Lstat(x.DataDirectory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return ErrState
	}
	r, err := os.OpenRoot(x.DataDirectory)
	if err != nil {
		return err
	}
	defer r.Close()
	opened, err := r.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		return ErrState
	}
	return sitecontrol.ClearOverride(ctx, r)
}
func (x *DockerExecutor) Handoff(ctx context.Context) error {
	s, err := x.private()
	if err != nil {
		return err
	}
	defer s.Close()
	done, err := s.handoffLease()
	if err != nil {
		return err
	}
	defer done()
	var h handoff
	if err = s.read("handoff.json", 16<<20, &h); err != nil {
		return err
	}
	var status Status
	if err = s.read("status.json", 8192, &status); err != nil {
		return err
	}
	if !status.valid() || status.State != "restarting" || status.CurrentSHA != h.Target || status.TargetSHA != x.Runtime || !x.validHandoff(h, x.Runtime) {
		return ErrState
	}
	e, err := x.engine(ctx)
	if err != nil {
		return err
	}
	defer e.Close()
	desired, err := e.Image(ctx, h.Image, h.Target, "updater")
	if err != nil {
		return err
	}
	var original Image
	if err = e.call(ctx, "GET", "/images/"+h.Snapshot.Image+"/json", nil, &original); err != nil {
		return err
	}
	if !release.ValidSHA(original.Config.Labels["frontiercloud.revision"]) {
		return ErrState
	}
	if _, err = e.Image(ctx, h.Snapshot.Image, original.Config.Labels["frontiercloud.revision"], "updater"); err != nil {
		return err
	}
	name := strings.TrimPrefix(h.Snapshot.Name, "/")
	if current, e2 := e.Inspect(ctx, name); e2 == nil {
		if current.ID != h.Snapshot.ID {
			if current.Image != desired.ID || current.label("frontiercloud.release-operation") != h.Target {
				return ErrState
			}
			if current.State.Status == "running" {
				return nil
			}
			if err = e.Start(ctx, current.ID); err == nil {
				return nil
			}
			return x.recoverHandoff(ctx, e, s, h, status, desired, err)
		}
	} else if !errors.Is(e2, ErrNotFound) {
		return e2
	}
	_, err = e.Replace(ctx, h.Snapshot, h.Image, h.Target)
	if err == nil {
		return nil
	}
	return x.recoverHandoff(ctx, e, s, h, status, desired, err)
}

// A failed handoff must not remove the only control agent. Restore only the
// exact saved native updater image/configuration; the already committed Web
// generation stays intact and public maintenance remains closed. A lost Docker
// start reply is first reconciled, so a running target is never rolled back.
func (x *DockerExecutor) recoverHandoff(parent context.Context, e *Engine, s *privateStore, h handoff, status Status, desired Image, cause error) error {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 4*time.Minute)
	defer cancel()
	name := strings.TrimPrefix(h.Snapshot.Name, "/")
	current, err := e.Inspect(ctx, name)
	if err == nil {
		if current.ID != h.Snapshot.ID && (current.label("frontiercloud.release-operation") != h.Target || current.label("com.docker.compose.project") != x.Project || current.label("com.docker.compose.service") != "updater") {
			return errors.Join(cause, ErrState)
		}
		if current.Image == desired.ID && current.State.Status == "running" {
			return nil
		}
		if err = e.Stop(ctx, current.ID); err != nil {
			return errors.Join(cause, err)
		}
	} else if !errors.Is(err, ErrNotFound) {
		return errors.Join(cause, err)
	}
	var old Image
	if err = e.call(ctx, "GET", "/images/"+h.Snapshot.Image+"/json", nil, &old); err != nil {
		return errors.Join(cause, err)
	}
	revision := old.Config.Labels["frontiercloud.revision"]
	if !release.ValidSHA(revision) {
		return errors.Join(cause, ErrState)
	}
	if _, err = e.Image(ctx, h.Snapshot.Image, revision, "updater"); err != nil {
		return errors.Join(cause, err)
	}
	var latest Status
	if err = s.read("status.json", 8192, &latest); err != nil {
		return errors.Join(cause, err)
	}
	if !latest.valid() || latest.State != "restarting" || latest.TargetSHA != status.TargetSHA || latest.CurrentSHA != h.Target {
		return errors.Join(cause, ErrState)
	}
	latest.State, latest.Phase, latest.Detail = "failed", "updater-restart-failed", "updater handoff failed; previous control runtime restored under maintenance"
	latest.RuntimeSHA, latest.CompletedAt, latest.UpdatedAt = revision, time.Now().Unix(), time.Now().Unix()
	if err = s.write("status.json", latest, 8192); err != nil {
		return errors.Join(cause, err)
	}
	if _, err = e.Replace(ctx, h.Snapshot, h.Snapshot.Image, h.Target); err != nil {
		return errors.Join(cause, err)
	}
	return cause
}
