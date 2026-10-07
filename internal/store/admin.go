package store

import "context"

type AdminAudit struct {
	SessionHash   string
	Action        string
	TargetCount   int
	SourceSummary string
	Result        string
	Detail        string
	ClientIP      string
	UserAgent     string
	RequestID     string
	TraceID       string
}
type AdminRepository interface {
	AppendAudit(context.Context, AdminAudit) error
}
