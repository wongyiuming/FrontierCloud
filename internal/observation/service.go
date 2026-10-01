package observation

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"net/netip"
	"sort"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/network"
	"github.com/wongyiuming/FrontierCloud/internal/security"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

var ErrRateLimit = errors.New("WebRTC observation rate limited")
var ErrObservation = errors.New("invalid WebRTC observation")
var failures = map[string]bool{"unsupported": true, "disabled": true, "timeout": true, "no_srflx": true, "ice_error": true}
var releaseReservation = redis.NewScript("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end; return 0")

type Service struct {
	repository store.ObservationRepository
	cache      *redis.Client
	cooldown   int
}
type Result struct {
	Status  string `json:"status"`
	Count   int    `json:"address_count"`
	Matches bool   `json:"matches_verified"`
	Outcome string `json:"outcome"`
}

func New(repo store.ObservationRepository, cache *redis.Client, cooldown int) *Service {
	return &Service{repo, cache, cooldown}
}
func (s *Service) Record(ctx context.Context, client string, addresses []string, failure string) (Result, error) {
	failure = strings.TrimSpace(failure)
	if failure != "" && !failures[failure] {
		return Result{}, ErrObservation
	}
	unique := map[string]bool{}
	normalized := []string{}
	for _, raw := range addresses {
		ip, err := network.Normalize(raw)
		if err != nil {
			return Result{}, ErrObservation
		}
		address, _ := netip.ParseAddr(ip)
		if address.IsUnspecified() || address.IsMulticast() {
			return Result{}, ErrObservation
		}
		if !unique[ip] {
			unique[ip] = true
			normalized = append(normalized, ip)
		}
		if len(normalized) > 8 {
			return Result{}, ErrObservation
		}
	}
	if len(normalized) == 0 && failure == "" {
		return Result{}, ErrObservation
	}
	sort.Strings(normalized)
	outcome := failure
	if outcome == "" {
		outcome = "ok"
	}
	token := make([]byte, 16)
	if _, err := rand.Read(token); err != nil {
		return Result{}, err
	}
	reservation := hex.EncodeToString(token)
	key := "webrtc:observation:" + client
	acquired := false
	if s.cache != nil {
		request, cancel := context.WithTimeout(ctx, time.Second)
		accepted, err := s.cache.SetNX(request, key, reservation, time.Duration(max(1, s.cooldown-1))*time.Second).Result()
		cancel()
		if err == nil {
			if !accepted {
				return Result{}, ErrRateLimit
			}
			acquired = true
		}
	}
	if err := s.repository.RecordObservations(ctx, client, normalized, outcome); err != nil {
		if acquired {
			release, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Second)
			_ = releaseReservation.Run(release, s.cache, []string{key}, reservation).Err()
			cancel()
		}
		return Result{}, err
	}
	return Result{Status: "recorded", Count: len(normalized), Matches: unique[client], Outcome: outcome}, nil
}
func (s *Service) List(ctx context.Context, f store.ObservationFilter) (store.ObservationSummary, store.ObservationFilter, error) {
	var err error
	f.PublicIP, err = security.FilterIP(f.PublicIP, f.MatchMode)
	if err != nil {
		return store.ObservationSummary{}, f, ErrObservation
	}
	f.WebRTCIP, err = security.FilterIP(f.WebRTCIP, f.MatchMode)
	if err != nil {
		return store.ObservationSummary{}, f, ErrObservation
	}
	if f.MatchMode == "fuzzy" {
		f.PageSize = min(50, f.PageSize)
	}
	value, err := s.repository.Observations(ctx, f)
	return value, f, err
}
