// Standalone testbench for rtl/bc1_shift_only_sync.v: drives one cell directly -- no TAP,
// no network -- with the stimulus-file-driven, pre-edge-sampling shape of tb_sib_cell.v.
//
// Stimulus file: one line of five integers each: "<trst_n> <pi> <si> <capture_dr> <shift_dr>".
`timescale 1ns/1ps

module tb_bc1_shift_only_sync;
    reg tck = 0;
    reg trst_n = 1;
    reg pi = 0;
    reg si = 0;
    reg capture_dr = 0;
    reg shift_dr = 0;
    wire so;

    bc1_shift_only_sync dut (
        .pi(pi),
        .si(si),
        .so(so),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .tck(tck),
        .trst_n(trst_n)
    );

    integer fd;
    integer code;
    integer trst_n_in, pi_in, si_in, capture_dr_in, shift_dr_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d",
                           trst_n_in, pi_in, si_in, capture_dr_in, shift_dr_in);
            if (code != 5) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                pi = pi_in[0];
                si = si_in[0];
                capture_dr = capture_dr_in[0];
                shift_dr = shift_dr_in[0];
                #1; // let combinational logic (including async reset) settle before sampling
                $display("TRACE,%0d", so);
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
