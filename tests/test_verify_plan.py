import unittest

from src.verify_plan import (
    VerifyPattern,
    VerifyStep,
    build_default_profile,
    evaluate_match,
    load_single_profile_from_text,
    load_verify_profiles_from_text,
    profile_to_yaml_text,
    render_template_text,
)


class VerifyPlanTests(unittest.TestCase):
    def test_load_profiles_from_config_text(self) -> None:
        text = """
verify_profiles:
  smoke:
    description: 启动冒烟测试
    serial:
      baudrate: 74880
    steps:
      - action: reset
      - action: expect
        timeout_ms: 4000
        match_mode: all
        patterns:
          - boot:
          - ready
"""
        profiles = load_verify_profiles_from_text(text)
        self.assertIn("smoke", profiles)
        self.assertEqual(profiles["smoke"].serial.baudrate, 74880)
        self.assertEqual(len(profiles["smoke"].steps), 2)

    def test_load_single_profile_accepts_wrapped_verify_profiles(self) -> None:
        text = """
verify_profiles:
  boot-check:
    steps:
      - action: wait
        duration_ms: 100
"""
        profile = load_single_profile_from_text("临时脚本", text)
        self.assertEqual(profile.name, "临时脚本")
        self.assertEqual(profile.steps[0].action, "wait")
        self.assertEqual(profile.steps[0].duration_ms, 100)

    def test_evaluate_match_supports_all_contains(self) -> None:
        step = VerifyStep(
            action="expect",
            match_mode="all",
            match_type="contains",
            patterns=[VerifyPattern("boot:"), VerifyPattern("ready")],
        )
        result = evaluate_match("boot: ok\nready\n", step)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.pending_patterns, [])

    def test_evaluate_match_supports_none_mode(self) -> None:
        step = VerifyStep(
            action="expect",
            match_mode="none",
            match_type="contains",
            patterns=[VerifyPattern("Guru Meditation"), VerifyPattern("panic")],
        )
        result = evaluate_match("boot complete\n", step)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.matched_patterns, [])

    def test_invalid_action_raises_value_error(self) -> None:
        text = """
steps:
  - action: jump
"""
        with self.assertRaises(ValueError):
            load_single_profile_from_text("bad", text)

    def test_default_profile_can_round_trip_yaml(self) -> None:
        profile = build_default_profile()
        text = profile_to_yaml_text(profile)
        loaded = load_single_profile_from_text(profile.name, text)
        self.assertEqual(loaded.serial.baudrate, profile.serial.baudrate)
        self.assertEqual(
            [step.action for step in loaded.steps],
            [step.action for step in profile.steps],
        )

    def test_capture_and_result_steps_can_round_trip_yaml(self) -> None:
        text = """
steps:
  - action: capture
    timeout_ms: 5000
    match_mode: all
    match_type: regex
    patterns:
      - name: version
        pattern: 'version[:= ]+([^\\r\\n]+)'
        group: 1
      - name: sn
        pattern: 'sn[:= ]+([A-Z0-9]+)'
        group: 1
  - action: set_result
    text: '版本={{version}} / SN={{sn}}'
"""
        profile = load_single_profile_from_text("capture-demo", text)
        self.assertEqual(profile.steps[0].action, "capture")
        self.assertEqual(profile.steps[0].patterns[0].name, "version")
        self.assertEqual(profile.steps[0].patterns[1].name, "sn")
        dumped = profile_to_yaml_text(profile)
        loaded = load_single_profile_from_text("capture-demo", dumped)
        self.assertEqual(loaded.steps[0].patterns[0].name, "version")
        self.assertEqual(loaded.steps[1].text, "版本={{version}} / SN={{sn}}")

    def test_capture_step_requires_parameter_name(self) -> None:
        text = """
steps:
  - action: capture
    match_type: regex
    patterns:
      - pattern: 'version[:= ]+([^\\r\\n]+)'
"""
        with self.assertRaises(ValueError):
            load_single_profile_from_text("bad-capture", text)

    def test_render_template_text_supports_captured_values(self) -> None:
        rendered = render_template_text(
            "版本={{version}} / SN={{sn}} / 端口={{port}}",
            {"version": "1.2.3", "sn": "ABC123", "port": "COM7"},
        )
        self.assertEqual(rendered, "版本=1.2.3 / SN=ABC123 / 端口=COM7")

    def test_wait_silence_and_retry_can_round_trip_yaml(self) -> None:
        text = """
steps:
  - action: wait_silence
    duration_ms: 400
    timeout_ms: 2000
    retry_count: 2
    retry_delay_ms: 150
  - action: fail
    text: '设备返回错误，终止脚本'
  - action: pass
    text: '设备状态正常，提前通过'
"""
        profile = load_single_profile_from_text("advanced-demo", text)
        self.assertEqual(profile.steps[0].action, "wait_silence")
        self.assertEqual(profile.steps[0].duration_ms, 400)
        self.assertEqual(profile.steps[0].retry_count, 2)
        self.assertEqual(profile.steps[0].retry_delay_ms, 150)
        self.assertEqual(profile.steps[1].action, "fail")
        self.assertEqual(profile.steps[2].action, "pass")
        dumped = profile_to_yaml_text(profile)
        loaded = load_single_profile_from_text("advanced-demo", dumped)
        self.assertEqual(loaded.steps[0].action, "wait_silence")
        self.assertEqual(loaded.steps[0].retry_count, 2)
        self.assertEqual(loaded.steps[2].text, "设备状态正常，提前通过")

    def test_wait_silence_timeout_must_cover_silence_duration(self) -> None:
        text = """
steps:
  - action: wait_silence
    duration_ms: 800
    timeout_ms: 500
"""
        with self.assertRaises(ValueError):
            load_single_profile_from_text("bad-wait-silence", text)


if __name__ == "__main__":
    unittest.main()
