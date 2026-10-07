package observation

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/redis/go-redis/v9"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	storeSQLite "github.com/wongyiuming/FrontierCloud/internal/store/sqlite"
)

func TestObservationNormalizationDoesNotChangeVerifiedIP(t *testing.T) {
	db, err := storeSQLite.Open(filepath.Join(t.TempDir(), "db.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	s := New(db.Observations(), nil, 30)
	for _, addresses := range [][]string{{"0.0.0.0"}, {"::"}, {"224.0.0.1"}, {"fe80::1%eth0"}, {"bad.example"}} {
		if _, err := s.Record(ctx, "198.51.100.1", addresses, ""); !errors.Is(err, ErrObservation) {
			t.Fatalf("invalid candidate accepted %v %v", addresses, err)
		}
	}
	if _, err := s.Record(ctx, "198.51.100.1", nil, ""); !errors.Is(err, ErrObservation) {
		t.Fatal("empty report accepted")
	}
	value, err := s.Record(ctx, "198.51.100.1", []string{"2001:0DB8::1", "2001:db8::1", "198.51.100.1"}, "")
	if err != nil || value.Count != 2 || !value.Matches {
		t.Fatalf("normalization: %+v %v", value, err)
	}
	value, err = s.Record(ctx, "198.51.100.1", nil, "timeout")
	if err != nil || value.Count != 0 || value.Matches || value.Outcome != "timeout" {
		t.Fatalf("failure: %+v %v", value, err)
	}
	listed, _, err := s.List(ctx, store.ObservationFilter{PublicIP: "198.51.100.1", View: "pairs", MatchMode: "exact", Page: 1, PageSize: 100})
	if err != nil || listed.Total != 3 {
		t.Fatalf("durable records: %+v %v", listed, err)
	}
}
func TestObservationRedisCooldownAndFailedTransactionRelease(t *testing.T) {
	url := os.Getenv("FRONTIERCLOUD_TEST_REDIS_URL")
	if url == "" {
		t.Skip("disposable Redis URL not configured")
	}
	options, err := redis.ParseURL(url)
	if err != nil {
		t.Fatal(err)
	}
	client := redis.NewClient(options)
	defer client.Close()
	db, err := storeSQLite.Open(filepath.Join(t.TempDir(), "db.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()
	if err := db.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	client.Del(ctx, "webrtc:observation:198.51.100.177")
	defer client.Del(ctx, "webrtc:observation:198.51.100.177")
	s := New(&failingObservation{db.Observations()}, client, 30)
	if _, err := s.Record(ctx, "198.51.100.177", nil, "timeout"); err == nil {
		t.Fatal("failed transaction accepted")
	}
	s.repository = db.Observations()
	if _, err := s.Record(ctx, "198.51.100.177", nil, "timeout"); err != nil {
		t.Fatal("failed transaction retained cooldown", err)
	}
	if _, err := s.Record(ctx, "198.51.100.177", nil, "timeout"); !errors.Is(err, ErrRateLimit) {
		t.Fatalf("cooldown bypass: %v", err)
	}
}

type failingObservation struct{ store.ObservationRepository }

func (*failingObservation) RecordObservations(context.Context, string, []string, string) error {
	return errors.New("injected database failure")
}
