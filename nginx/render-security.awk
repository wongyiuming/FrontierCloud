# Accept only the versioned data format; malformed input preserves old rules.
BEGIN { invalid = 0 }
NR == 1 {
    if ($0 != "# frontiercloud-ip-security-v1") invalid = 1
    next
}
{
    if (NF != 2 || $1 !~ /^[0-9a-fA-F:.]+$/ || $2 !~ /^[0-9]+$/) {
        invalid = 1
        next
    }
    if ($2 == 0 || $2 > now) print $1 " 1;"
}
END { if (NR == 0 || invalid) exit 1 }
