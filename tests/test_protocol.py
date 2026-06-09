import unittest

from daq_gui.protocol import (
    DataFrame,
    Message,
    SingleRead,
    Status,
    command,
    parse_line,
    raw_to_signed,
)


class ProtocolTests(unittest.TestCase):
    def test_parses_data_frame(self) -> None:
        parsed = parse_line("DATA,42,H,1,2,3,4,L,5,6,7,8")
        self.assertEqual(
            parsed,
            DataFrame(sample_counter=42, high=(1, 2, 3, 4), low=(5, 6, 7, 8)),
        )
        self.assertEqual(parsed.difference, (-4, -4, -4, -4))

    def test_parses_status(self) -> None:
        parsed = parse_line("STATUS operating=1,streamAdc=0,visDac=120")
        self.assertIsInstance(parsed, Status)
        self.assertEqual(parsed.values["visDac"], "120")

    def test_parses_single_read(self) -> None:
        parsed = parse_line("SINGLE_VOLTS,1.0,-2.0,3.5,0.0")
        self.assertEqual(parsed, SingleRead("SINGLE_VOLTS", (1.0, -2.0, 3.5, 0.0)))

    def test_rejects_malformed_data(self) -> None:
        parsed = parse_line("DATA,42,H,1,2")
        self.assertIsInstance(parsed, Message)
        self.assertEqual(parsed.level, "error")

    def test_signed_adc_conversion(self) -> None:
        self.assertEqual(raw_to_signed(0x7FFF), 32767)
        self.assertEqual(raw_to_signed(0x8000), -32768)
        self.assertEqual(raw_to_signed(0xFFFF), -1)

    def test_formats_command(self) -> None:
        self.assertEqual(command(10, 100), "10,100\n")


if __name__ == "__main__":
    unittest.main()
