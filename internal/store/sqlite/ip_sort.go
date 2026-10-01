package sqlite

import (
	"database/sql/driver"
	"net/netip"

	driverSQLite "modernc.org/sqlite"
)

func init() {
	// Equivalent network-order bytes, not lexical text order: IPv4 before
	// IPv6, and 10.2 before 10.11. Available on every pooled connection.
	driverSQLite.MustRegisterDeterministicScalarFunction("INET6_ATON", 1, func(_ *driverSQLite.FunctionContext, args []driver.Value) (driver.Value, error) {
		text, ok := args[0].(string)
		if !ok {
			return nil, nil
		}
		address, err := netip.ParseAddr(text)
		if err != nil || address.Zone() != "" {
			return nil, nil
		}
		if address.Is4() {
			bytes := address.As4()
			return bytes[:], nil
		}
		bytes := address.As16()
		return bytes[:], nil
	})
}
