// Standalone testbench for rtl/instrument_write_clr.v, in tb_sib_cell.v's
// stimulus-file-driven, pre-edge-sampling shape.
//
// Stimulus file: one line of seven integers each:
// "<trst_n> <clr_n> <si> <select> <capture_dr> <shift_dr> <update_dr>".
`timescale 1ns/1ps

module tb_instrument_write_clr;
    reg tck = 0;
    reg trst_n = 1;
    reg clr_n = 1;
    reg si = 0;
    reg select = 0;
    reg capture_dr = 0;
    reg shift_dr = 0;
    reg update_dr = 0;
    wire so;
    wire pin_out;

    instrument_write_clr dut (
        .si(si),
        .so(so),
        .pin_out(pin_out),
        .select(select),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .update_dr(update_dr),
        .tck(tck),
        .trst_n(trst_n),
        .clr_n(clr_n)
    );

    integer fd;
    integer code;
    integer trst_n_in, clr_n_in, si_in, select_in, capture_dr_in, shift_dr_in, update_dr_in;
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
                           trst_n_in, clr_n_in, si_in, select_in,
                           capture_dr_in, shift_dr_in, update_dr_in);
            if (code != 7) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                clr_n = clr_n_in[0];
                si = si_in[0];
                select = select_in[0];
                capture_dr = capture_dr_in[0];
                shift_dr = shift_dr_in[0];
                update_dr = update_dr_in[0];
                #1; // let the async clears settle before sampling
                $display("TRACE,%0d,%0d", so, pin_out);
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
