// Hand-authored synthesizable TAP core: 16-state FSM + instruction register + BYPASS/IDCODE
// data registers (implementation_plan.md §7 Stage 2). State encoding, opcode assignment, and
// every capture/shift/update timing decision here mirror warptap.tap_model.TapModel exactly --
// that correspondence is what tests/test_tap_fsm_cross_sim.py cross-simulates and checks, not
// a promise kept by construction alone.
//
// SAMPLE_PRELOAD/EXTEST decode correctly and drive capture_dr/shift_dr/update_dr correctly
// (pure functions of `state`, identical regardless of which instruction is active), but this
// module owns no data register for them -- external_dr_tdo is the port Stage 3's
// boundary-scan register plugs into. This module's FSM/IR/mux logic never needs to change
// when that happens.
module tap_core #(
    parameter IR_WIDTH = 4,
    parameter [IR_WIDTH-1:0] OPCODE_EXTEST = 4'b0000,
    parameter [IR_WIDTH-1:0] OPCODE_SAMPLE_PRELOAD = 4'b0010,
    parameter [IR_WIDTH-1:0] OPCODE_IDCODE = 4'b0001,
    parameter HAS_IDCODE = 1,
    parameter [31:0] IDCODE_VALUE = 32'h1A5A5003
) (
    input  wire tck,
    input  wire tms,
    input  wire tdi,
    input  wire trst_n,
    output reg  tdo,

    // Debug/verification ports -- not part of the JTAG pin interface, but what
    // tests/test_tap_fsm_cross_sim.py samples to diff against TapModel.
    output wire [3:0] tap_state,
    output wire [IR_WIDTH-1:0] current_instruction,
    output wire capture_dr,
    output wire shift_dr,
    output wire update_dr,

    // Stage 3's boundary-scan register plugs in here; this module never inspects it.
    input  wire external_dr_tdo
);
    // State encoding matches warptap.tap_fsm.TapState's IntEnum values exactly, so a
    // cross-simulation trace comparison needs no translation table.
    localparam [3:0]
        TEST_LOGIC_RESET = 4'h0, RUN_TEST_IDLE   = 4'h1, SELECT_DR_SCAN = 4'h2,
        CAPTURE_DR       = 4'h3, SHIFT_DR        = 4'h4, EXIT1_DR       = 4'h5,
        PAUSE_DR         = 4'h6, EXIT2_DR        = 4'h7, UPDATE_DR      = 4'h8,
        SELECT_IR_SCAN   = 4'h9, CAPTURE_IR      = 4'hA, SHIFT_IR       = 4'hB,
        EXIT1_IR         = 4'hC, PAUSE_IR        = 4'hD, EXIT2_IR       = 4'hE,
        UPDATE_IR        = 4'hF;

    localparam [IR_WIDTH-1:0] BYPASS_OPCODE = {IR_WIDTH{1'b1}};
    // Deliberately equal to OPCODE_IDCODE -- see warptap.tap_model.CAPTURE_IR_PATTERN's
    // docstring for why.
    localparam [IR_WIDTH-1:0] CAPTURE_IR_PATTERN = OPCODE_IDCODE;

    reg [3:0] state;
    reg [IR_WIDTH-1:0] ir_shift;
    reg [IR_WIDTH-1:0] current_instruction_r;
    reg [31:0] idcode_shift;
    reg bypass_bit;

    assign tap_state = state;
    assign current_instruction = current_instruction_r;
    assign capture_dr = (state == CAPTURE_DR);
    assign shift_dr   = (state == SHIFT_DR);
    assign update_dr  = (state == UPDATE_DR);

    // Next-state function, 1:1 with warptap.tap_fsm.TRANSITIONS. The two entries most
    // prone to an off-by-one transcription error are EXIT2_DR/EXIT2_IR with TMS=0: they
    // return to SHIFT_DR/SHIFT_IR, not back to CAPTURE_DR/CAPTURE_IR.
    reg [3:0] next_state_c;
    always @(*) begin
        case (state)
            TEST_LOGIC_RESET: next_state_c = tms ? TEST_LOGIC_RESET : RUN_TEST_IDLE;
            RUN_TEST_IDLE:    next_state_c = tms ? SELECT_DR_SCAN   : RUN_TEST_IDLE;
            SELECT_DR_SCAN:   next_state_c = tms ? SELECT_IR_SCAN   : CAPTURE_DR;
            CAPTURE_DR:       next_state_c = tms ? EXIT1_DR         : SHIFT_DR;
            SHIFT_DR:         next_state_c = tms ? EXIT1_DR         : SHIFT_DR;
            EXIT1_DR:         next_state_c = tms ? UPDATE_DR        : PAUSE_DR;
            PAUSE_DR:         next_state_c = tms ? EXIT2_DR         : PAUSE_DR;
            EXIT2_DR:         next_state_c = tms ? UPDATE_DR        : SHIFT_DR;
            UPDATE_DR:        next_state_c = tms ? SELECT_DR_SCAN   : RUN_TEST_IDLE;
            SELECT_IR_SCAN:   next_state_c = tms ? TEST_LOGIC_RESET : CAPTURE_IR;
            CAPTURE_IR:       next_state_c = tms ? EXIT1_IR         : SHIFT_IR;
            SHIFT_IR:         next_state_c = tms ? EXIT1_IR         : SHIFT_IR;
            EXIT1_IR:         next_state_c = tms ? UPDATE_IR        : PAUSE_IR;
            PAUSE_IR:         next_state_c = tms ? EXIT2_IR         : PAUSE_IR;
            EXIT2_IR:         next_state_c = tms ? UPDATE_IR        : SHIFT_IR;
            UPDATE_IR:        next_state_c = tms ? SELECT_DR_SCAN   : RUN_TEST_IDLE;
            default:          next_state_c = TEST_LOGIC_RESET;
        endcase
    end

    // Sequential: state register plus every IR/DR action gated on the state we are IN
    // during this TCK period (the *current*, pre-edge `state` -- action strobes describe
    // activity happening throughout the cycle spent resident in that state, mirroring
    // TapModel.tick()'s old-state-gated dispatch exactly).
    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            state <= TEST_LOGIC_RESET;
            ir_shift <= {IR_WIDTH{1'b0}};
            current_instruction_r <= HAS_IDCODE ? OPCODE_IDCODE : BYPASS_OPCODE;
        end else begin
            case (state)
                CAPTURE_IR: ir_shift <= CAPTURE_IR_PATTERN;
                SHIFT_IR:   ir_shift <= {tdi, ir_shift[IR_WIDTH-1:1]};
                UPDATE_IR: begin
                    if (ir_shift == OPCODE_EXTEST)
                        current_instruction_r <= OPCODE_EXTEST;
                    else if (ir_shift == OPCODE_SAMPLE_PRELOAD)
                        current_instruction_r <= OPCODE_SAMPLE_PRELOAD;
                    else if (HAS_IDCODE && ir_shift == OPCODE_IDCODE)
                        current_instruction_r <= OPCODE_IDCODE;
                    else
                        current_instruction_r <= BYPASS_OPCODE;
                end
                CAPTURE_DR: begin
                    if (HAS_IDCODE && current_instruction_r == OPCODE_IDCODE)
                        idcode_shift <= IDCODE_VALUE;
                    else if (current_instruction_r == BYPASS_OPCODE)
                        bypass_bit <= 1'b0;
                    // EXTEST/SAMPLE_PRELOAD: the external DR's owner (Stage 3) is
                    // responsible for capturing on its own capture_dr strobe.
                end
                SHIFT_DR: begin
                    if (HAS_IDCODE && current_instruction_r == OPCODE_IDCODE)
                        idcode_shift <= {tdi, idcode_shift[31:1]};
                    else if (current_instruction_r == BYPASS_OPCODE)
                        bypass_bit <= tdi;
                end
                // UPDATE_DR: BYPASS/IDCODE have no parallel output to latch.
                default: ;
            endcase
            state <= next_state_c;
        end
    end

    // TDO mux: combinational from the *current* (pre-edge) register contents, so it
    // presents the bit about to be shifted out throughout the cycle -- matches
    // TapModel.tick() computing its returned tdo before mutating its own registers.
    // Only meaningful while resident in Shift-IR/Shift-DR; 0 elsewhere.
    always @(*) begin
        if (state == SHIFT_IR) begin
            tdo = ir_shift[0];
        end else if (state == SHIFT_DR) begin
            if (HAS_IDCODE && current_instruction_r == OPCODE_IDCODE)
                tdo = idcode_shift[0];
            else if (current_instruction_r == BYPASS_OPCODE)
                tdo = bypass_bit;
            else
                tdo = external_dr_tdo;
        end else begin
            tdo = 1'b0;
        end
    end

endmodule
