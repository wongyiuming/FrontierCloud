package main

import (
	"bytes"
	"testing"
)

func TestMasterMigrationRejectsMissingAndAmbiguousAuthorityBeforeIO(t *testing.T) {
	for _, arguments := range [][]string{nil, {"serve"}, {"admit"}, {"snapshot", "--node-id", "invalid"}, {"admit", "--node-id", "11111111111111111111111111111111", "--manifest", "/proof/recovered.json", "--mysql-socket", "/var/run/mysqld/mysqld.sock"}, {"snapshot", "--node-id", "11111111111111111111111111111111", "--manifest", "relative", "--mysql-socket", "/var/run/mysqld/mysqld.sock"}} {
		var output bytes.Buffer
		if err := masterMigrationCommand(arguments, &output); err == nil || output.Len() != 0 {
			t.Fatal("incomplete operator authority accepted", arguments, err)
		}
	}
}

func TestFollowerMigrationRejectsAdmissionAndMissingProofBeforeIO(t *testing.T) {
	for _, arguments := range [][]string{nil, {"admit"}, {"snapshot"}, {"snapshot", "--node-id", "invalid"}} {
		var output bytes.Buffer
		if err := followerMigrationCommand(arguments, &output); err == nil || output.Len() != 0 {
			t.Fatal("unproven follower authority accepted", err)
		}
	}
	var output bytes.Buffer
	if err := followerToSQLiteCommand(nil, &output); err == nil || output.Len() != 0 {
		t.Fatal("follower conversion without recovery proof accepted", err)
	}
}
