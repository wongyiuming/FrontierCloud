package business_test

import (
	"context"
	"sync"
	"testing"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

func TestObservationConcurrentSummaryNullFailuresAndGrouping(t *testing.T) {
	db := database(t)
	repo := db.Observations()
	ctx := context.Background()
	client := "198.51.100.144"
	var workers sync.WaitGroup
	failures := make(chan error, 16)
	for range 16 {
		workers.Go(func() {
			if err := repo.RecordObservations(ctx, client, []string{"2001:db8::144", client}, "ok"); err != nil {
				failures <- err
			}
		})
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	if err := repo.RecordObservations(ctx, client, nil, "timeout"); err != nil {
		t.Fatal(err)
	}
	f := store.ObservationFilter{PublicIP: client, MatchMode: "exact", View: "pairs", Page: 1, PageSize: 100}
	value, err := repo.Observations(ctx, f)
	if err != nil || value.Total != 3 || len(value.Items) != 3 {
		t.Fatalf("pairs: %+v %v", value, err)
	}
	for _, item := range value.Items {
		want := int64(16)
		if item.WebRTCIP == nil {
			want = 1
			if item.Outcome != "timeout" {
				t.Fatal("failure observation lost")
			}
		}
		if item.Count != want || item.WebRTCIP != nil && *item.WebRTCIP == client && item.MatchingCount != 16 {
			t.Fatalf("lost summary increments: %+v", item)
		}
	}
	f.View = "public"
	value, err = repo.Observations(ctx, f)
	if err != nil || value.Total != 1 || len(value.Groups) != 1 || value.Groups[0].Count != 33 || value.Groups[0].RelationCount != 3 || len(value.Groups[0].Relations) != 3 {
		t.Fatalf("public grouping: %+v %v", value, err)
	}
	f.View = "webrtc"
	value, err = repo.Observations(ctx, f)
	if err != nil || value.Total != 2 || len(value.Groups) != 2 {
		t.Fatalf("observed grouping: %+v %v", value, err)
	}
	f.View = "pairs"
	f.MatchMode = "fuzzy"
	f.PublicIP = "198.51.100.14"
	f.WebRTCIP = "2001:db8"
	f.PageSize = 50
	value, err = repo.Observations(ctx, f)
	if err != nil || value.Total != 1 || value.Items[0].Count != 16 {
		t.Fatalf("filtered summary: %+v %v", value, err)
	}
}
