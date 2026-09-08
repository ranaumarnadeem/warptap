// Standalone testbench for two chained rtl/sib_cell.v instances (nested-SIB plan, Phase 0):
// outer.nested_si -> inner.si, outer.nested_select -> inner.select, inner.so -> outer.nested_so
// -- exactly the wiring a hierarchy SIB's own recursive insertion would produce, proven here
// against the real RTL primitive directly, before any package source changes exist to build
// on. Mirrors tb_sib_cell.v's stimulus-file-driven, pre-edge-sampling shape exactly.
//
// Stimulus file: one line of seven integers each:
// "<trst_n> <outer_select> <outer_si> <inner_nested_so> <capture_dr> <shift_dr> <update_dr>".
`timescale 1ns/1ps

module tb_sib_cell_nested;
    reg tck = 0;
    reg trst_n = 1;
    reg outer_select = 0;
    reg outer_si = 0;
    reg inner_nested_so = 0;
    reg capture_dr = 0;
    reg shift_dr = 0;
    reg update_dr = 0;

    wire outer_so, outer_nested_si, outer_nested_select;
    wire inner_so, inner_nested_si, inner_nested_select;

    sib_cell outer (
        .si(outer_si),
        .so(outer_so),
        .nested_so(inner_so),
        .nested_si(outer_nested_si),
        .nested_select(outer_nested_select),
        .select(outer_select),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .update_dr(update_dr),
        .tck(tck),
        .trst_n(trst_n)
    );

    sib_cell inner (
        .si(outer_nested_si),
        .so(inner_so),
        .nested_so(inner_nested_so),
        .nested_si(inner_nested_si),
        .nested_select(inner_nested_select),
        .select(outer_nested_select),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .update_dr(update_dr),
        .tck(tck),
        .trst_n(trst_n)
    );

    integer fd;
    integer code;
    integer trst_n_in, outer_select_in, outer_si_in, inner_nested_so_in;
    integer capture_dr_in, shift_dr_in, update_dr_in;
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
                            trst_n_in, outer_select_in, outer_si_in, inner_nested_so_in,
                            capture_dr_in, shift_dr_in, update_dr_in);
            if (code != 7) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                outer_select = outer_select_in[0];
                outer_si = outer_si_in[0];
                inner_nested_so = inner_nested_so_in[0];
                capture_dr = capture_dr_in[0];
                shift_dr = shift_dr_in[0];
                update_dr = update_dr_in[0];
                #1; // let combinational logic (including async reset) settle before sampling
                $display("TRACE,%0d,%0d", outer_so, inner_so);
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
