package store

import "context"

type ObservationFilter struct {
	PublicIP, WebRTCIP, MatchMode, View string
	Page, PageSize                      int
}
type Observation struct {
	ClientIP      string  `json:"client_ip"`
	WebRTCIP      *string `json:"webrtc_ip"`
	Count         int64   `json:"observation_count"`
	MatchingCount int64   `json:"matching_count"`
	FirstSeen     string  `json:"first_seen"`
	LastSeen      string  `json:"last_seen"`
	Outcome       string  `json:"outcomes"`
}
type ObservationGroup struct {
	Key           string        `json:"key"`
	RelationCount int           `json:"relation_count"`
	Count         int64         `json:"observation_count"`
	FirstSeen     string        `json:"first_seen"`
	LastSeen      string        `json:"last_seen"`
	Relations     []Observation `json:"relations"`
}
type ObservationSummary struct {
	Items  []Observation
	Groups []ObservationGroup
	Total  int
}
type ObservationRepository interface {
	RecordObservations(context.Context, string, []string, string) error
	Observations(context.Context, ObservationFilter) (ObservationSummary, error)
}
