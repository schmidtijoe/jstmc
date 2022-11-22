import logging
import types
from jstmc import options
import numpy as np
import pypulseq as pp
import tqdm

logModule = logging.getLogger(__name__)


def set_on_grad_raster_time(time: float, system: pp.Opts):
    return np.ceil(time / system.grad_raster_time) * system.grad_raster_time


def load_external_rf(rf_file) -> np.ndarray:
    pass


class Acquisition:
    def __init__(self, params: options.SequenceParameters, system: pp.Opts):
        self.params = params
        self.system = system
        # grad
        self.read_grad: types.SimpleNamespace = types.SimpleNamespace()
        self.read_grad_pre: types.SimpleNamespace = types.SimpleNamespace()
        # adc
        self.adc: types.SimpleNamespace = types.SimpleNamespace()
        # phase
        self.phase_grad_areas: np.ndarray = np.zeros(0)
        self.phase_grad_pre_adc: types.SimpleNamespace = types.SimpleNamespace()
        self.phase_grad_post_adc: types.SimpleNamespace = types.SimpleNamespace()
        # timing
        self.t_phase: float = 0.0  # time needed for longest phase enc -> all phase enc
        self.t_read_pre: float = 0.0
        # init
        self._make_read_gradients()
        self._set_phase_areas()

    # methods private
    def _make_read_gradients(self):
        acquisition_window = set_on_grad_raster_time(self.params.acquisitionTime, system=self.system)
        self.read_grad = pp.make_trapezoid(
            channel=self.params.read_dir,
            flat_area=self.params.deltaK_read * self.params.resolutionNRead,
            flat_time=acquisition_window,  # given in [s] via options
            system=self.system
        )
        self.read_grad_pre = pp.make_trapezoid(
            channel=self.params.read_dir,
            area=self.read_grad.area / 2,
            system=self.system
        )
        self.t_read_pre = pp.calc_duration(self.read_grad_pre)
        # set adc
        self.adc = pp.make_adc(
            num_samples=self.params.resolutionNRead,
            delay=self.read_grad.rise_time,
            duration=self.read_grad.flat_time,
            system=self.system)

    def _set_phase_areas(self):
        self.phase_grad_areas = (np.arange(self.params.resolutionNPhase) - self.params.resolutionNPhase / 2) * \
                                self.params.deltaK_phase
        # build longest phase gradient
        gPhase_max = pp.make_trapezoid(
            channel=self.params.phase_dir,
            area=np.max(self.phase_grad_areas),
            system=self.system
        )
        # calculate time needed for biggest phase grad
        self.t_phase = set_on_grad_raster_time(pp.calc_duration(gPhase_max), self.system)

    def set_phase_grads(self, idx_phase):
        if np.abs(self.phase_grad_areas[idx_phase]) > 0:
            # calculate phase step
            self.phase_grad_pre_adc = pp.make_trapezoid(
                channel=self.params.phase_dir,
                area=self.phase_grad_areas[idx_phase],
                duration=self.t_phase,
                system=self.system
            )
            self.phase_grad_post_adc = pp.make_trapezoid(
                channel=self.params.phase_dir,
                area=-self.phase_grad_areas[idx_phase],
                duration=self.t_phase,
                system=self.system
            )
        else:
            self.phase_grad_pre_adc = pp.make_delay(self.t_phase)
            self.phase_grad_post_adc = pp.make_delay(self.t_phase)

    def reset_read_grad_pre(self, t_read_grad_pre: float):
        self.read_grad_pre = pp.make_trapezoid(
            channel=self.params.read_dir,
            area=self.read_grad_pre.area,
            duration=t_read_grad_pre,
            system=self.system
        )

    def get_t_read_grad_pre(self) -> float:
        return self.t_read_pre

    def reset_t_phase(self, t_phase: float):
        self.t_phase = t_phase

    def get_t_phase(self) -> float:
        return self.t_phase


class SliceGradPulse:
    def __init__(self, params: options.SequenceParameters, system: pp.Opts, t_xy_grad: float,
                 excitation_refocus_flag: bool):
        self.params = params
        self.system = system
        self.t_xy_grad = t_xy_grad
        self.exci_refoc_flag = excitation_refocus_flag

        # slice
        self.slice_grad: types.SimpleNamespace = types.SimpleNamespace()
        self.slice_grad_ru: types.SimpleNamespace = types.SimpleNamespace()
        # if excitation we rephase slice grad and spoil, if refocusing this is the spoiler grad
        self.slice_grad_re_spoil: types.SimpleNamespace = types.SimpleNamespace()
        if self.exci_refoc_flag:
            # if excitation we pre wind
            self.slice_grad_prewind: types.SimpleNamespace = pp.make_trapezoid(
                'z',
                area=self.params.excitationPreMoment,
                max_slew=0.8 * self.system.max_slew
            )
            flip_angle_rad = self.params.excitationRadFA
            phase_rad = self.params.excitationRadRfPhase
            time_bw_prod = self.params.excitationTimeBwProd
            duration = self.params.excitationDuration * 1e-6
        else:
            # if refocusing we spoil
            self.slice_grad_spoil_post: types.SimpleNamespace = types.SimpleNamespace()
            self.slice_grad_spoil_pre: types.SimpleNamespace = types.SimpleNamespace()
            flip_angle_rad = self.params.refocusingRadFA
            phase_rad = self.params.refocusingRadRfPhase
            time_bw_prod = self.params.refocusingTimeBwProd
            duration = self.params.refocusingDuration * 1e-6

        slice_thickness = self.params.resolutionSliceThickness * 1e-3
        duration = set_on_grad_raster_time(duration, system=self.system)

        # rf
        self.rf: types.SimpleNamespace = types.SimpleNamespace()
        # delay
        self.delay: types.SimpleNamespace = pp.make_delay(0.0)

        # timing
        self.t_re_spoil: float = 0.0

        # init
        # build rf gradient pulse
        self._make_rf_grad_pulse(
            flip_angle_rad=flip_angle_rad,
            phase_rad=phase_rad,
            time_bw_prod=time_bw_prod,
            duration=duration,
            slice_thickness=slice_thickness
        )
        if self.exci_refoc_flag:
            self._recalculate_rephase_grad()
        else:
            # build spoiler gradients
            self._make_spoiler_gradient()
        self._merge_grads()  # merge slice gradients to continuous waveform

    def _make_rf_grad_pulse(self, flip_angle_rad: float, phase_rad: float, time_bw_prod: float,
                            duration: float, slice_thickness: float):

        if self.exci_refoc_flag:
            use = "excitation"
            apodization = 0.0
        else:
            use = "refocusing"
            apodization = 0.0
        self.rf, self.slice_grad, slice_grad_re = pp.make_sinc_pulse(
            flip_angle=flip_angle_rad,
            phase_offset=phase_rad,
            delay=0.0,
            apodization=apodization,
            time_bw_product=time_bw_prod,
            duration=duration,  # given in [us] via options
            max_slew=0.8 * self.system.max_slew,
            system=self.system,
            slice_thickness=slice_thickness,
            return_gz=True,
            use=use
        )
        if self.exci_refoc_flag:
            self.slice_grad_re_spoil = slice_grad_re

    def _recalculate_rephase_grad(self):
        # calculate spoil grad area -> cast thickness from mm to m
        spoil_area = self.params.spoilerScaling * 1e3 / self.params.resolutionSliceThickness
        # reset rephaser
        self.slice_grad_re_spoil = pp.make_trapezoid(
            channel='z',
            area=spoil_area + self.slice_grad_re_spoil.area,
            system=self.system
        )
        self.t_re_spoil = set_on_grad_raster_time(pp.calc_duration(self.slice_grad_re_spoil), system=self.system)
        if self._check_timing_changed():
            # if we need to adjust timing do so
            self.slice_grad_re_spoil = pp.make_trapezoid(
                channel='z',
                area=self.slice_grad_re_spoil.area,
                system=self.system,
                duration=self.t_re_spoil
            )

    def _make_spoiler_gradient(self):
        self.slice_grad_re_spoil = pp.make_trapezoid(
            channel='z',
            area=self.params.spoilerScaling * 1e3 / self.params.resolutionSliceThickness,
            # slice thickness given in mm
            system=self.system
        )
        self.t_re_spoil = set_on_grad_raster_time(pp.calc_duration(self.slice_grad_re_spoil), system=self.system)
        if self._check_timing_changed():
            # if we need to adjust timing do so
            self.slice_grad_re_spoil = pp.make_trapezoid(
                channel='z',
                area=self.slice_grad_re_spoil.area,
                # slice thickness given in mm
                system=self.system,
                duration=self.t_re_spoil
            )

    def _check_timing_changed(self) -> bool:
        # set longer time after excitation and optimize gradients to timing
        if self.t_re_spoil > self.t_xy_grad:
            logModule.debug(f"Rephaser / Spoiler time restricting: {self.t_re_spoil * 1e3:.2f} ms")
            return False
        else:
            logModule.debug(f"Read or Phase gradient time restricting: {self.t_xy_grad * 1e3:.2f} ms")
            self.t_re_spoil = set_on_grad_raster_time(self.t_xy_grad, system=self.system)
            return True

    def _merge_grads(self):
        # --- merge excitation and rephaser at edges ---
        # we want to interpolate between the slice selection ramp down and the rephasing ramp up
        t_rd_ru = self.slice_grad.fall_time + self.slice_grad_re_spoil.rise_time
        interpol_gradient = (self.slice_grad_re_spoil.amplitude - self.slice_grad.amplitude) / t_rd_ru * \
                            self.rf.ringdown_time + self.slice_grad.amplitude
        interpol_gradient_rd_pre = (self.slice_grad_re_spoil.amplitude - self.slice_grad.amplitude) / t_rd_ru * \
                                   self.rf.delay + self.slice_grad.amplitude

        rise = self.rf.delay
        amp = self.slice_grad.amplitude
        flat_time = self.slice_grad.flat_time

        # interpolation to spoiler + flat + interpolation to spoiler
        self.slice_grad = pp.make_extended_trapezoid(
            'z',
            amplitudes=np.array([interpol_gradient_rd_pre, amp, amp, interpol_gradient]),
            times=np.array([0, rise, rise + flat_time, rise + flat_time + self.rf.ringdown_time])
        )

        # rephase
        if self.exci_refoc_flag:
            # merge prewind / slice select
            t_arr = np.array([
                0.0,
                self.slice_grad_prewind.rise_time,
                self.slice_grad_prewind.rise_time + self.slice_grad_prewind.flat_time,
                self.slice_grad_prewind.rise_time + self.slice_grad_prewind.flat_time + rise,
                self.slice_grad_prewind.rise_time + self.slice_grad_prewind.flat_time + rise + flat_time,
                self.slice_grad_prewind.rise_time + self.slice_grad_prewind.flat_time + rise + flat_time + self.rf.ringdown_time
            ])
            amps = np.array([
                0,
                self.slice_grad_prewind.amplitude,
                self.slice_grad_prewind.amplitude,
                amp,
                amp,
                interpol_gradient
            ])
            self.slice_grad_ru = pp.make_extended_trapezoid(
                'z',
                amplitudes=amps,
                times=t_arr
            )
            self.rf.delay = rise + self.slice_grad_prewind.rise_time + self.slice_grad_prewind.flat_time

            # merge gradient re/spoiler
            t_arr = np.array([
                0.0,
                rise + self.slice_grad_re_spoil.rise_time - self.rf.ringdown_time,
                self.slice_grad_re_spoil.flat_time + rise + self.slice_grad_re_spoil.rise_time - self.rf.ringdown_time,
                self.slice_grad_re_spoil.flat_time + rise + 2 * self.slice_grad_re_spoil.rise_time - self.rf.ringdown_time
            ])
            amps = np.array([
                self.slice_grad_ru.last,
                self.slice_grad_re_spoil.amplitude,
                self.slice_grad_re_spoil.amplitude,
                0.0
            ])
            re_spoil_amp = self.slice_grad_re_spoil.amplitude
            self.slice_grad_re_spoil = pp.make_extended_trapezoid('z', amplitudes=amps, times=t_arr)
            self.slice_grad_re_spoil.amplitude = re_spoil_amp
            logModule.debug(f"excitation grad: {1e3 * self.slice_grad.last / self.system.gamma:.2f} mT/m")
            logModule.debug(
                f"excitation rephasing grad: {1e3 * self.slice_grad_re_spoil.amplitude / self.system.gamma:.2f} mT/m"
            )
        else:
            self.slice_grad_ru = pp.make_extended_trapezoid(
                'z',
                amplitudes=np.array([
                    0,
                    amp,
                    amp,
                    interpol_gradient
                ]),
                times=np.array([
                    0.0,
                    rise,
                    rise + flat_time,
                    rise + flat_time + self.rf.ringdown_time
                ])
            )
            # spoilers
            spoil_amps_pre = np.array([
                0.0,
                self.slice_grad_re_spoil.amplitude,
                self.slice_grad_re_spoil.amplitude,
                interpol_gradient_rd_pre
            ])
            spoil_timings_pre = np.array([
                0.0,
                self.slice_grad_re_spoil.rise_time,
                self.slice_grad_re_spoil.rise_time + self.slice_grad_re_spoil.flat_time,
                self.slice_grad_re_spoil.rise_time + self.slice_grad_re_spoil.flat_time + self.slice_grad_re_spoil.fall_time
            ])

            spoil_amps_post = spoil_amps_pre[::-1].copy()
            spoil_amps_post[0] = self.slice_grad_ru.last
            spoil_timings_post = np.array([
                0.0,
                self.slice_grad_re_spoil.rise_time - self.rf.ringdown_time,
                self.slice_grad_re_spoil.rise_time + self.slice_grad_re_spoil.flat_time - self.rf.ringdown_time,
                self.slice_grad_re_spoil.rise_time + self.slice_grad_re_spoil.flat_time + self.slice_grad_re_spoil.fall_time - self.rf.ringdown_time
            ])

            self.slice_grad_spoil_pre = pp.make_extended_trapezoid(
                'z',
                amplitudes=spoil_amps_pre,
                times=spoil_timings_pre
            )
            self.slice_grad_spoil_post = pp.make_extended_trapezoid(
                'z',
                amplitudes=spoil_amps_post,
                times=spoil_timings_post
            )
            logModule.debug(f"refocusing grad: {1e3 * self.slice_grad.last / self.system.gamma:.2f} mT/m")
            logModule.debug(
                f"refocusing spoiling grad: {1e3 * self.slice_grad_re_spoil.amplitude / self.system.gamma:.2f} mT/m"
            )
        self.slice_grad.amplitude = amp

    def check_post_slice_selection_timing(self):
        return not self._check_timing_changed()

    def get_timing_post_slice_selection(self):
        return self.t_re_spoil


class SequenceBlockEvents:
    def __init__(self, seq: options.Sequence):
        self.seq = seq
        # ___ define all block event vars ___
        # Acquisition
        logModule.info("Setting up Acquisition")
        # Excitation
        self.acquisition = Acquisition(params=self.seq.params, system=self.seq.ppSys)
        logModule.info("Setting up Excitation")
        self.excitation = SliceGradPulse(
            params=self.seq.params,
            system=self.seq.ppSys,
            t_xy_grad=self.acquisition.get_t_read_grad_pre(),
            excitation_refocus_flag=True
        )
        if self.excitation.check_post_slice_selection_timing():
            logModule.info(f"Excitation rephase timing longer than readout prephasing, readjusting readout pre")
            self.acquisition.reset_read_grad_pre(self.excitation.get_timing_post_slice_selection())
        # Refocusing
        logModule.info("Setting up Refocusing")
        self.refocusing = SliceGradPulse(
            params=self.seq.params,
            system=self.seq.ppSys,
            t_xy_grad=self.acquisition.get_t_phase(),
            excitation_refocus_flag=False
        )
        if self.refocusing.check_post_slice_selection_timing():
            logModule.info(f"Spoiling timing longer than phase encode, readjusting phase enc timing")
            self.acquisition.reset_t_phase(self.refocusing.get_timing_post_slice_selection())

        # Timing
        self.t_duration_echo_train: float = 0.0
        self.t_delay_slice: types.SimpleNamespace = pp.make_delay(0.0)
        self._calculate_min_esp()

        # k space
        self.k_start: int = -1
        self.k_end: int = -1
        self.k_indexes: np.ndarray = np.zeros((self.seq.params.ETL, self.seq.params.numberOfOuterLines), dtype=int)
        self.sampling_pattern: list = []
        # slice loop
        numSlices = self.seq.params.resolutionNumSlices
        self.z = np.zeros((2, int(np.ceil(numSlices / 2))))
        self.trueSliceNum = np.zeros(numSlices)

    def _write_emc_info(self) -> dict:
        emc_dict = {
            "gammaHz": self.seq.specs.gamma,
            "ETL": self.seq.params.ETL,
            "ESP": self.seq.params.ESP,
            "bw": self.seq.params.bandwidth,
            "gradMode": "Normal",
            "excitationAngle": self.seq.params.excitationRadFA / np.pi * 180.0,
            "gradientExcitation": self._set_grad_for_emc(self.excitation.slice_grad.amplitude),
            "durationExcitation": self.seq.params.excitationDuration,
            "gradientExcitationRephase": self._set_grad_for_emc(self.excitation.slice_grad_re_spoil.amplitude),
            "durationExcitationRephase": self.excitation.t_re_spoil * 1e6,
            "gradientExcitationVerse1": 0.0,
            "gradientExcitationVerse2": 0.0,
            "durationExcitationVerse1": 0.0,
            "durationExcitationVerse2": 0.0,
            "refocusAngle": self.seq.params.refocusingRadFA / np.pi * 180.0,
            "gradientRefocus": self._set_grad_for_emc(self.refocusing.slice_grad.amplitude),
            "durationRefocus": self.seq.params.refocusingDuration,
            "gradientCrush": self._set_grad_for_emc(self.refocusing.slice_grad.amplitude),
            "durationCrush": self.refocusing.t_re_spoil * 1e6,
            "gradientRefocusVerse1": 0.0,
            "gradientRefocusVerse2": 0.0,
            "durationRefocusVerse1": 0.0,
            "durationRefocusVerse2": 0.0
        }
        return emc_dict

    def _set_grad_for_emc(self, grad):
        return -1e3 / self.seq.specs.gamma * grad

    def get_sampling_pattern(self) -> list:
        return self.sampling_pattern

    def _calculate_min_esp(self):
        # find minimal echo spacing

        # between excitation and refocus = esp / 2 -> rf with delay?
        timing_excitation_refocus = pp.calc_duration(self.excitation.rf) / 2 + \
                                    self.excitation.t_re_spoil + \
                                    pp.calc_duration(self.refocusing.rf) / 2
        timing_excitation_refocus = set_on_grad_raster_time(timing_excitation_refocus, system=self.seq.ppSys)

        # between refocus and adc = esp / 2
        timing_refoucs_adc = pp.calc_duration(self.refocusing.rf) / 2 + \
                             self.refocusing.t_re_spoil + \
                             pp.calc_duration(self.acquisition.read_grad) / 2
        timing_refoucs_adc = set_on_grad_raster_time(timing_refoucs_adc, system=self.seq.ppSys)

        # diff
        t_diff = set_on_grad_raster_time(np.abs(timing_refoucs_adc - timing_excitation_refocus), system=self.seq.ppSys)
        # choose longer time as half echo spacing
        if timing_refoucs_adc > timing_excitation_refocus:
            esp = 2 * timing_refoucs_adc
            self.excitation.delay = pp.make_delay(t_diff)
        else:
            esp = 2 * timing_excitation_refocus
            self.refocusing.delay = pp.make_delay(t_diff)
        self.seq.params.set_esp(esp)

        logModule.info(f"Found minimum TE: {esp * 1e3:.2f} ms")

        self.t_duration_echo_train = set_on_grad_raster_time(
            pp.calc_duration(self.excitation.slice_grad_prewind) +
            pp.calc_duration(self.excitation.slice_grad_ru) +  # before middle of rf
            (pp.calc_duration(self.excitation.rf) - pp.calc_duration(self.excitation.slice_grad_ru)) / 2 +
            self.seq.params.ETL * esp +  # whole TE train
            pp.calc_duration(self.acquisition.read_grad) / 2 +  # half of last read gradient
            pp.calc_duration(self.refocusing.slice_grad_re_spoil),  # spoiler
            system=self.seq.ppSys
        )
        logModule.info(f"echo train length: {self.t_duration_echo_train * 1e3:.2f} ms")

    def _calculate_num_slices(self):
        # calculate how many slices can be accommodated
        numSlices = np.min([
            self.seq.params.resolutionNumSlices,
            int(np.floor(self.seq.params.TR * 1e-3 / self.t_duration_echo_train))
        ])
        logModule.info(
            f"{int(np.floor(self.seq.params.TR * 1e-3 / self.t_duration_echo_train))} "
            f"Slices can be accommodated in one TR; "
            f"{self.seq.params.resolutionNumSlices} were desired"
        )
        if numSlices < self.seq.params.resolutionNumSlices:
            logModule.info(f"need concatenation!")

        delay_slice_time = set_on_grad_raster_time(
            self.seq.params.TR * 1e-3 / numSlices - self.t_duration_echo_train,
            system=self.seq.ppSys
        )
        self.t_delay_slice = pp.make_delay(delay_slice_time)
        logModule.info(f"Delay between slices: {self.t_delay_slice.delay * 1e3:.2f} ms")

    def _set_k_space(self):
        # calculate center of k space and indexes for full sampling band
        k_central_phase = round(self.seq.params.resolutionNPhase / 2)
        k_half_central_lines = round(self.seq.params.numberOfCentralLines / 2)
        # set indexes for start and end of full k space center sampling
        self.k_start = k_central_phase - k_half_central_lines
        self.k_end = k_central_phase + k_half_central_lines

        # The rest of the lines we will use tse style phase step blip between the echoes of one echo train
        # Trying random sampling, ie. pick random line numbers for remaining indices
        # calculate indexes
        k_remaining = np.concatenate((
            np.arange(0, self.k_start),
            np.arange(self.k_end, self.seq.params.resolutionNPhase)
        ))
        # build array with dim [num_slices, num_outer_lines] to sample different random scheme per slice
        for idx_echo in range(self.seq.params.ETL):
            k_indices = np.sort(np.random.choice(
                k_remaining,
                size=self.seq.params.numberOfOuterLines,
                replace=False))
            self.k_indexes[idx_echo] = k_indices

        # old code
        # # -> acceleration increases with number of contrasts
        # k_end_low = self.k_start - self.seq.params.ETL + 1
        # # use partial fourier 6/8 -> aka 3/4
        # k_end_high = self.seq.params.resolutionNPhase - self.seq.params.ETL + 1
        # # calculate indexes
        # self.k_indexes = np.concatenate((np.arange(0, k_end_low, self.seq.params.accelerationFactor),
        #                                  np.arange(self.k_end, k_end_high, self.seq.params.accelerationFactor)))

    def _set_delta_slices(self):
        # multi-slice
        # want to go through the slices alternating from beginning and middle
        delta_z = self.seq.params.resolutionSliceThickness * self.seq.params.resolutionNumSlices * \
                  (1 + self.seq.params.resolutionSliceGap / 100.0) * 1e-3  # cast from % / cast from mm
        numSlices = self.seq.params.resolutionNumSlices
        self.z.flat[:numSlices] = np.linspace((-delta_z / 2), (delta_z / 2), numSlices)
        # reshuffle slices mid+1, 1, mid+2, 2, ...
        self.z = self.z.transpose().flatten()[:numSlices]
        # find reshuffled slice numbers
        for idx_slice_num in range(numSlices):
            z_val = self.z[idx_slice_num]
            z_pos = np.where(np.unique(self.z) == z_val)[0][0]
            self.trueSliceNum[idx_slice_num] = z_pos

    def _apply_slice_offset(self, idx_slice: int, is_excitation: bool = True):
        if is_excitation:
            # excitation
            grad_amplitude = self.excitation.slice_grad.amplitude
            rad_phase_pulse = self.seq.params.excitationRadRfPhase
            rf = self.excitation.rf
        else:
            # refocus
            grad_amplitude = self.refocusing.slice_grad.amplitude
            rad_phase_pulse = self.seq.params.refocusingRadRfPhase
            rf = self.refocusing.rf
        # apply slice offset
        freq_offset = grad_amplitude * self.z[idx_slice]
        phase_offset = rad_phase_pulse - 2 * np.pi * freq_offset * pp.calc_rf_center(rf)[0]
        return freq_offset, phase_offset

    def _add_blocks_excitation_first_read(self, phase_idx: int, slice_idx: int):
        # set phase grads
        self.acquisition.set_phase_grads(idx_phase=phase_idx)

        # excitation
        self.seq.ppSeq.add_block(self.excitation.rf, self.excitation.slice_grad_ru)
        # rephasing
        self.seq.ppSeq.add_block(self.excitation.slice_grad_re_spoil, self.acquisition.read_grad_pre)
        # delay if necessary
        if self.excitation.delay.delay > 1e-6:
            self.seq.ppSeq.add_block(self.excitation.delay)

        # refocus
        self.seq.ppSeq.add_block(self.refocusing.rf, self.refocusing.slice_grad_ru)
        # spoiling phase encode, delay if necessary
        self.seq.ppSeq.add_block(self.refocusing.slice_grad_spoil_post, self.acquisition.phase_grad_pre_adc)
        # delay if necessary
        if self.refocusing.delay.delay > 1e-6:
            self.seq.ppSeq.add_block(self.refocusing.delay)

        # read
        self.seq.ppSeq.add_block(self.acquisition.read_grad, self.acquisition.adc)

        # write sampling pattern
        sampling_index = {"pe_num": phase_idx, "slice_num": int(self.trueSliceNum[slice_idx]), "echo_num": 0}
        self.sampling_pattern.append(sampling_index)

    def _add_blocks_refocusing_adc(self, phase_idx: int, slice_idx: int, tse_style: bool = False):
        for contrast_idx in np.arange(1, self.seq.params.ETL):
            # delay if necessary
            if self.refocusing.delay.delay > 1e-6:
                self.seq.ppSeq.add_block(self.refocusing.delay)

            # dephase, spoil
            self.seq.ppSeq.add_block(self.acquisition.phase_grad_post_adc, self.refocusing.slice_grad_spoil_pre)

            # refocus
            self.seq.ppSeq.add_block(self.refocusing.rf, self.refocusing.slice_grad)

            # spoil phase encode
            # jump to next line if tse style acquisition
            if tse_style:
                idx_phase = self.k_indexes[contrast_idx, phase_idx]
            else:
                idx_phase = phase_idx
            # set phase
            self.acquisition.set_phase_grads(idx_phase=idx_phase)
            self.seq.ppSeq.add_block(self.acquisition.phase_grad_pre_adc, self.refocusing.slice_grad_spoil_post)
            # read
            self.seq.ppSeq.add_block(self.acquisition.read_grad, self.acquisition.adc)

            # write sampling pattern
            sampling_index = {"pe_num": idx_phase, "slice_num": int(self.trueSliceNum[slice_idx]), "echo_num": contrast_idx}
            self.sampling_pattern.append(sampling_index)

        # spoil end
        self.seq.ppSeq.add_block(
            self.acquisition.phase_grad_post_adc,
            self.refocusing.slice_grad_re_spoil,
            self.acquisition.read_grad_pre
        )
        self.seq.ppSeq.add_block(self.t_delay_slice)

    def _loop_central_mc(self):
        logModule.info(f"Central lines")
        # through phase encodes
        line_bar = tqdm.trange(self.seq.params.numberOfCentralLines, desc="phase encodes")
        for idx_n in line_bar:  # We have N phase encodes for all ETL contrasts
            # we start at lower end and move through central lines
            idx_phase = self.k_start + idx_n
            for idx_slice in range(self.seq.params.resolutionNumSlices):
                # apply slice offset
                self.excitation.rf.freq_offset, self.excitation.rf.phase_offset = self._apply_slice_offset(
                    idx_slice=idx_slice,
                    is_excitation=True
                )
                self.refocusing.rf.freq_offset, self.refocusing.rf.phase_offset = self._apply_slice_offset(
                    idx_slice=idx_slice,
                    is_excitation=False
                )

                # excitation to first read
                self._add_blocks_excitation_first_read(phase_idx=idx_phase, slice_idx=idx_slice)

                # refocusing blocks
                self._add_blocks_refocusing_adc(phase_idx=idx_phase, slice_idx=idx_slice, tse_style=False)

    def _loop_acc_tse(self):
        logModule.info(f"TSE acc lines")
        # The rest of the lines we will use tse style phase step blip between the echoes of one echo train
        # -> acceleration increases with number of contrasts

        line_bar = tqdm.trange(self.seq.params.numberOfOuterLines, desc="phase encodes")
        for idx_n in line_bar:  # We have N phase encodes for all ETL contrasts
            for idx_slice in range(self.seq.params.resolutionNumSlices):
                # apply slice offset
                self.excitation.rf.freq_offset, self.excitation.rf.phase_offset = self._apply_slice_offset(
                    idx_slice=idx_slice,
                    is_excitation=True
                )
                self.refocusing.rf.freq_offset, self.refocusing.rf.phase_offset = self._apply_slice_offset(
                    idx_slice=idx_slice,
                    is_excitation=False
                )

                # for idx_slice in range(num_slices):
                idx_phase = self.k_indexes[0, idx_n]
                # add blocks excitation til first read
                self._add_blocks_excitation_first_read(phase_idx=idx_phase, slice_idx=idx_slice)

                # add blocks for refocussing pulses, tse style
                self._add_blocks_refocusing_adc(phase_idx=idx_n, slice_idx=idx_slice, tse_style=True)

    def build(self):
        # calculate number of slices
        self._calculate_num_slices()
        # set k-space sampling indices
        self._set_k_space()
        # set positions for slices
        self._set_delta_slices()

        # loop through central multi contrast building blocks
        self._loop_central_mc()
        # loop through tse style outer k-space
        self._loop_acc_tse()

    def get_seq(self):
        # write info into seq obj
        self._write_emc_info()
        return self.seq

    def get_emc_info(self) -> dict:
        return self._write_emc_info()
