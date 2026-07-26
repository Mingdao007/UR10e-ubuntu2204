from publish_step5d_autotune_plot import completed_trial, parameter_png_name


def bundle():
    return {
        "capture": {
            "terminal_reason": 1,
            "returned_safe": True,
            "stage25_complete_s": 60.1,
            "safe_closure_evidence": {"trial_token_match": True},
        },
        "evaluation": {"complete_bins": 550},
        "trial": {
            "trial_id": 3,
            "campaign": {"required_bins": 550},
            "candidate": {
                "force_p_gain": 0.001,
                "force_i_gain": 1e-5,
                "force_damping": 7.0,
            },
            "execution_profile": {
                "normal_max_rate_rad_s": 0.01,
                "host_qdot_slew_rad_s2": 0.1,
                "tp_speedj_accel_rad_s2": 0.2,
            },
        },
    }


def test_parameter_png_name_is_readable_and_repeat_safe():
    assert parameter_png_name(bundle()) == (
        "P=0.001_I=1e-05_D=7_nf=0.01_slew=0.1_a=0.2_trial=0003.png"
    )


def test_completed_trial_requires_reason1_bins_and_safe_closure():
    row = bundle()
    assert completed_trial(row)
    row["capture"]["returned_safe"] = False
    assert not completed_trial(row)
