// Standalone testbench for rtl/sib_cell.v (implementation_plan.md §7 Stage 4, testing layer
// 2): drives one sib_cell instance directly -- no network, no instrument -- to prove its
// self-capture/scan-splice-mux/select-gating behavior in isolation, before any network
// insertion exists to build on. Mirrors tb_tap_core.v's stimulus-file-driven, pre-edge-
// sampling shape exactly.
//
// Stimulus file: one line of seven integers each:
// "<trst_n> <select> <si> <nested_so> <capture_dr> <shift_dr> <update_dr>".
`timescale 1ns/1ps

module tb_sib_cell;
    reg tck = 0;
    reg trst_n = 1;
    reg select = 0;
    reg si = 0;
    reg nested_so = 0;
    reg capture_dr = 0;
    reg shift_dr = 0;
    reg update_dr = 0;
    wire so;
    wire nested_si;
    wire nested_select;

    sib_cell dut (
        .si(si),
        .so(so),
        .nested_so(nested_so),
        .nested_si(nested_si),
        .nested_select(nested_select),
        .select(select),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .update_dr(update_dr),
        .tck(tck),
        .trst_n(trst_n)
    );

    integer fd;
    integer code;
    integer trst_n_in, select_in, si_in, nested_so_in, capture_dr_in, shift_dr_in, update_dr_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d %d %d",
                            trst_n_in, select_in, si_in, nested_so_in,
                            capture_dr_in, shift_dr_in, update_dr_in);
            if (code != 7) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                select = select_in[0];
                si = si_in[0];
                nested_so = nested_so_in[0];
                capture_dr = capture_dr_in[0];
                shift_dr = shift_dr_in[0];
                update_dr = update_dr_in[0];
                #1; // let combinational logic (including async reset) settle before sampling
                $display("TRACE,%0d,%0d,%0d", so, nested_si, nested_select);
                tck = 1;
                #1;
                tck = 0;
                #1;
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
