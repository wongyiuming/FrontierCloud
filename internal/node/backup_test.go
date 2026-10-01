package node

import "testing"

func TestBackupDueDailySuccessAndBoundedRetry(t *testing.T) {
	for _, test := range []struct {
		success, attempt, now int64
		due                   bool
	}{
		{0, 0, 100_000, true}, {100_000, 100_000, 100_001, false}, {100_000, 100_000, 186_400, true},
		{100_000, 100_001, 100_300, false}, {100_000, 100_001, 100_301, true}, {200_000, 0, 100_000, false},
	} {
		if due := backupDue(test.success, test.attempt, test.now); due != test.due {
			t.Fatal(test, due)
		}
	}
}
