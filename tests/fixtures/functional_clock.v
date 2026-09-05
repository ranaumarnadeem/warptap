// Fixture for real functional-clock-pulse cross-sim (implementation_plan.md §7 Stage 14).
// Deliberately isolates a SEPARATE functional clock (sysclk) from the JTAG TAP's own tck --
// every prior fixture in this project ties clk=tck (the same physical clock drives both the
// TAP and whatever functional logic exists); `captured` here is clocked by sysclk ALONE,
// proving warptap.tap_ir.PulsePin's own independent-clock-domain design against real,
// distinctly-clocked hardware, not just a self-consistent Python/STIL model.
module functional_clock (
    input  wire rst_n,
    input  wire sysclk,
    input  wire stim_in,
    output wire captured_out
);
    reg captured;

    always @(posedge sysclk or negedge rst_n) begin
        if (!rst_n)
            captured <= 1'b0;
        else
            captured <= stim_in;
    end

    assign captured_out = captured;
endmodule
