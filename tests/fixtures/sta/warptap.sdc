# Timing constraints for a warptap-inserted design mapped to IHP SG13G2 (units: ns, pF).
#
# Read by sta.tcl, after the caller has set:
#   tck_period         the test clock's period
#   functional_clocks  {port period ...}: the design's own clock inputs and their periods
#
# TCK. IEEE 1149.1 has the tester change TDI and TMS, and release TRST, on the falling edge of
# TCK, and sample TDO on the rising edge; warptap's TAP launches TDO (and tdo_oe) on the falling
# edge, so TDO's path to the tester is half a cycle. The tester's own skew is a tenth of a
# period each way.
create_clock -name tck -period $tck_period [get_ports tck]
set tester_skew [expr {0.1 * $tck_period}]
set_input_delay -clock tck -clock_fall $tester_skew [get_ports {tdi tms trst_n}]
set_output_delay -clock tck $tester_skew [get_ports tdo]
foreach pin {tdo_oe} {
    # tdo_drive="enable_port" brings tap_core's tdo_oe out for the pad that drives TDO.
    set port [get_ports -quiet $pin]
    if {$port != ""} { set_output_delay -clock tck $tester_skew $port }
}

# The design's own clocks. warptap's instruments are shifted, captured and updated on TCK and
# hand the design static levels; nothing in the network synchronizes them to a functional clock,
# and no access relies on a timed path from one domain to the other: a PDL program writes, lets
# the design's clock run, then reads. So each functional clock is asynchronous to TCK, and the
# paths between them are not checked here. Paths inside a functional domain are checked against
# its period, but none of its inputs or outputs is constrained: they belong to the design.
foreach {port period} $functional_clocks {
    create_clock -name $port -period $period [get_ports $port]
    set_clock_groups -asynchronous -group [get_clocks tck] -group [get_clocks $port]
}

set_propagated_clock [all_clocks]
# The inputs come from pads, modeled as the library's strongest buffer. Nothing here buffers a
# high-fanout net (no placement, no clock tree), so TCK, TRST and a design's reset each drive
# every flop they reach directly.
set_driving_cell -lib_cell sg13g2_buf_16 -pin X [all_inputs]
set_load 0.01 [all_outputs]
