// Package security enforces durable IP policy. Redis is deliberately not a
// second source of truth: indexed database lookups also work after Redis loss.
package security

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/netip"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/config"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type Service struct {
	settings   config.Config
	repository store.SecurityRepository
	exempt     []netip.Prefix
	root       *os.Root
}

func New(settings config.Config, repo store.SecurityRepository) (*Service, error) {
	s := &Service{settings: settings, repository: repo}
	for _, raw := range settings.SecurityExemptNetworks {
		prefix, err := netip.ParsePrefix(strings.TrimSpace(raw))
		if err != nil {
			return nil, fmt.Errorf("invalid SECURITY_EXEMPT_NETWORKS: %w", err)
		}
		s.exempt = append(s.exempt, prefix.Masked())
	}
	root, err := os.OpenRoot(settings.DataRoot)
	if err != nil {
		return nil, err
	}
	s.root = root
	if err := root.Mkdir(".ip-security", 0755); err != nil && !errors.Is(err, os.ErrExist) {
		root.Close()
		return nil, err
	}
	info, err := root.Lstat(".ip-security")
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		root.Close()
		return nil, errors.New("unsafe IP security directory")
	}
	if err := root.Chmod(".ip-security", info.Mode().Perm()|0o055); err != nil {
		root.Close()
		return nil, err
	}
	return s, nil
}
func (s *Service) Close() error { return s.root.Close() }
func (s *Service) Exempt(ip string) bool {
	address, err := netip.ParseAddr(ip)
	if err != nil {
		return false
	}
	for _, prefix := range s.exempt {
		if prefix.Contains(address) {
			return true
		}
	}
	return false
}
func (s *Service) Block(ctx context.Context, ip string) (*store.IPBan, error) {
	if s.Exempt(ip) {
		return nil, nil
	}
	return s.repository.IPBlock(ctx, ip)
}
func (s *Service) Invalid(ctx context.Context, ip, method, path, ua string, audit store.AdminAudit) error {
	if s.Exempt(ip) {
		return nil
	}
	_, err := s.repository.RecordInvalidAPI(ctx, ip, method, path, ua, s.settings.SecurityInvalidLimit, s.settings.SecurityInvalidWindow, audit)
	return err
}
func (s *Service) Policy(ctx context.Context, raw, action, note string, audit store.AdminAudit) (*store.IPBan, string, error) {
	ip, err := network.Normalize(raw)
	if err != nil {
		return nil, "", err
	}
	note = strings.TrimSpace(note)
	if action == "reban" || action == "permanent_ban" {
		if note == "" || len([]rune(note)) > 255 || s.Exempt(ip) {
			return nil, ip, store.ErrPolicy
		}
	}
	if len([]rune(note)) > 255 {
		note = string([]rune(note)[:255])
	}
	ban, err := s.repository.SetIPPolicy(ctx, ip, action, note, audit)
	if err != nil {
		return nil, ip, err
	}
	// Commit is authoritative even if edge publication must be retried. Backend
	// enforcement remains live; the periodic publisher repairs the projection.
	if err := s.Publish(ctx, false); err != nil {
		slog.Error("IP edge policy publication deferred", "error", err)
	}
	return ban, ip, nil
}

var ipFragment = regexp.MustCompile(`^[0-9a-f:.]+$`)

func FilterIP(value, mode string) (string, error) {
	if mode != "exact" && mode != "fuzzy" {
		return "", store.ErrPolicy
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	if mode == "exact" {
		return network.Normalize(value)
	}
	value = strings.ToLower(value)
	if len(value) < 3 || len(value) > 45 || !ipFragment.MatchString(value) {
		return "", store.ErrPolicy
	}
	return value, nil
}
func (s *Service) Summary(ctx context.Context, f store.SecurityFilter) (store.SecuritySummary, store.SecurityFilter, error) {
	ip, err := FilterIP(f.IP, f.MatchMode)
	if err != nil {
		return store.SecuritySummary{}, f, err
	}
	f.IP = ip
	f.Window = s.settings.SecurityInvalidWindow
	if f.MatchMode == "fuzzy" {
		f.PageSize = min(50, f.PageSize)
	}
	value, err := s.repository.SecuritySummary(ctx, f)
	return value, f, err
}
func (s *Service) Publish(ctx context.Context, force bool) error {
	if info, err := s.root.Lstat(".ip-security/publisher.lock"); err == nil && !info.Mode().IsRegular() {
		return errors.New("unsafe policy publisher lease")
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	f, err := s.root.OpenFile(".ip-security/publisher.lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	release, err := filelease.Acquire(ctx, f, true)
	if err != nil {
		return err
	}
	defer release()
	generation, published, err := s.repository.EdgeProjection(ctx)
	if err != nil {
		return err
	}
	if info, err := s.root.Lstat(".ip-security/active-bans.tsv"); errors.Is(err, os.ErrNotExist) {
		force = true
	} else if err != nil {
		return err
	} else if !info.Mode().IsRegular() {
		return errors.New("unsafe edge snapshot")
	}
	if !force && generation == published {
		return nil
	}
	bans, generation, err := s.repository.EdgeBans(ctx)
	if err != nil {
		return err
	}
	sort.Slice(bans, func(i, j int) bool {
		a, _ := netip.ParseAddr(bans[i].IP)
		b, _ := netip.ParseAddr(bans[j].IP)
		return a.Compare(b) < 0
	})
	var content strings.Builder
	content.WriteString("# frontiercloud-ip-security-v1\n")
	for _, ban := range bans {
		ip, err := network.Normalize(ban.IP)
		if err != nil {
			return err
		}
		if s.Exempt(ip) {
			continue
		}
		expiry := ban.ExpiresAt.Unix()
		if ban.Permanent {
			expiry = 0
		}
		fmt.Fprintf(&content, "%s\t%d\n", ip, expiry)
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return err
	}
	name := ".ip-security/.snapshot-" + hex.EncodeToString(random) + ".tmp"
	f, err = s.root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	defer s.root.Remove(name)
	_, err = f.WriteString(content.String())
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := s.root.Rename(name, ".ip-security/active-bans.tsv"); err != nil {
		return err
	}
	if err := syncEdgeDirectory(s.root); err != nil {
		return err
	}
	return s.repository.AcknowledgeEdge(ctx, generation)
}
func (s *Service) Run(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			attempt, cancel := context.WithTimeout(ctx, 5*time.Second)
			err := s.Publish(attempt, false)
			cancel()
			if err != nil && ctx.Err() == nil {
				slog.Error("IP edge policy refresh failed", "error", err)
			}
		}
	}
}
func (s *Service) Ready(ctx context.Context) error {
	generation, published, err := s.repository.EdgeProjection(ctx)
	if err != nil {
		return err
	}
	if generation != published {
		return errors.New("IP edge policy is pending publication")
	}
	info, err := s.root.Lstat(".ip-security/active-bans.tsv")
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("IP edge snapshot is unsafe")
	}
	return nil
}
