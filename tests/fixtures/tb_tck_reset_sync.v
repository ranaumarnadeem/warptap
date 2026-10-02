// Standalone testbench for rtl/tck_reset_sync.v, in tb_sib_cell.v's stimulus-file-driven,
// pre-edge-sampling shape.
//
// Stimulus file: one line of two integers each: "<trst_n> <chip_rst_n>".
`timescale 1ns/1ps

module tb_tck_reset_sync;
    reg tck = 0;
    reg trst_n = 1;
    reg chip_rst_n = 1;
    wire clr_n;

    tck_reset_sync dut (
        .tck(tck),
        .trst_n(trst_n),
        .chip_rst_n(chip_rst_n),
        .clr_n(clr_n)
    );

    integer fd;
    integer code;
    integer trst_n_in, chip_rst_n_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d", trst_n_in, chip_rst_n_in);
            if (code != 2) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                chip_rst_n = chip_rst_n_in[0];
                #1; // let the async clear settle before sampling
                $display("TRACE,%0d", clr_n);
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
