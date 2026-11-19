# OpenSTA run for a warptap-inserted design mapped to IHP SG13G2.
#
# The caller sets: slow_lib fast_lib (liberty files), netlist (a list of Verilog files), top,
# sdc (warptap.sdc), tck_period and functional_clocks (both read by the SDC), and report (a
# file for the full reports).
#
# Two scenes: "slow" (1.08 V, 125 C) and "fast" (1.32 V, -40 C). Setup matters at the slow one
# and hold at the fast one; every check is run at both. TRST is asynchronous, so the recovery
# and removal of the flops' reset pins are checked too (sta_recovery_removal_checks_enabled is
# 1 by default). Each result is printed on a line of its own, "WARPTAP <name> <value>".

read_liberty $slow_lib
read_liberty $fast_lib
foreach file $netlist { read_verilog $file }
link_design $top
read_sdc $sdc
define_scene slow -liberty $slow_lib
define_scene fast -liberty $fast_lib

foreach {scene min_max} {slow max fast max slow min fast min} {
    puts "WARPTAP ${min_max}_slack_$scene [sta::worst_slack -scene $scene -$min_max]"
    puts "WARPTAP ${min_max}_tns_$scene [sta::total_negative_slack -scene $scene -$min_max]"
}

report_checks -path_delay max -scenes slow -format full_clock_expanded -digits 3 > $report
report_checks -path_delay min -scenes fast -format full_clock_expanded -digits 3 >> $report
report_check_types -verbose -max_delay -min_delay -recovery -removal -min_pulse_width -min_period >> $report
# Every timing check kind, at both scenes, violators only: the test wants none.
report_check_types -violators -max_delay -min_delay -recovery -removal -min_pulse_width -min_period > ${report}.violators
# Design rules too (slew, capacitance, fanout), reported but not a timing check: the netlist has
# no placement, buffering or clock tree.
report_check_types -violators -max_slew -max_capacitance -max_fanout > ${report}.drv
puts "WARPTAP done 1"
