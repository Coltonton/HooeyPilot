from cereal import car
from common.conversions import Conversions as CV
from common.numpy_fast import mean
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_CTRL
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from selfdrive.car.interfaces import CarStateBase
from selfdrive.car.toyota.values import ToyotaFlags, CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, TSS2_CAR, RADAR_ACC_CAR, EPS_SCALE
from common.params import Params, put_nonblocking
import time
from math import floor

# dp
DP_ACCEL_ECO = 0
DP_ACCEL_NORMAL = 1
DP_ACCEL_SPORT = 2

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR_PACKET"]["GEAR"]
    self.eps_torque_scale = EPS_SCALE[CP.carFingerprint] / 100.

    # On cars with cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]
    # the signal is zeroed to where the steering angle is at start.
    # Need to apply an offset as soon as the steering angle measurements are both received
    self.accurate_steer_angle_seen = False
    self.angle_offset = FirstOrderFilter(None, 60.0, DT_CTRL, initialized=False)

    self.low_speed_lockout = False
    self.acc_type = 1

    # dp
    #self.read_distance_lines = 0
    #self.read_distance_lines_init = False
    #self.distance = 0
    #self.read_lkas_btn = 0
    #self.read_lkas_btn_init = False

    self.dp_toyota_zss = Params().get_bool('dp_toyota_zss')
    self.dp_accel_profile = None
    self.dp_accel_profile_prev = None
    self.dp_accel_profile_init = False

    #self.dp_toyota_fp_btn_link = Params().get_bool('dp_toyota_fp_btn_link')
    self.dp_toyota_ap_btn_link = Params().get_bool('dp_toyota_ap_btn_link')
    #self.dp_toyota_lkas_btn_link = Params().get_bool('dp_toyota_lkas_btn_link')

  def update(self, cp, cp_cam):
    ret = car.CarState.new_message()

    ret.doorOpen = any([cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_FR"],
                        cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RL"], cp.vl["BODY_CONTROL_STATE"]["DOOR_OPEN_RR"]])
    ret.seatbeltUnlatched = cp.vl["BODY_CONTROL_STATE"]["SEATBELT_DRIVER_UNLATCHED"] != 0
    ret.parkingBrake = cp.vl["BODY_CONTROL_STATE"]["PARKING_BRAKE"] == 1

    ret.brakePressed = cp.vl["BRAKE_MODULE"]["BRAKE_PRESSED"] != 0
    #ret.brakeHoldActive = cp.vl["ESP_CONTROL"]["BRAKE_HOLD_ACTIVE"] == 1
    ret.brakeLights = bool(cp.vl["ESP_CONTROL"]['BRAKE_LIGHTS_ACC'] or cp.vl["BRAKE_MODULE"]["BRAKE_PRESSED"] != 0)
    if self.CP.enableGasInterceptor:
      ret.gas = (cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS"] + cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS2"]) // 2
      ret.gasPressed = ret.gas > 805
    else:
      # TODO: find a new, common signal
      msg = "GAS_PEDAL_HYBRID" if (self.CP.flags & ToyotaFlags.HYBRID) else "GAS_PEDAL"
      ret.gas = cp.vl[msg]["GAS_PEDAL"]
      ret.gasPressed = cp.vl["PCM_CRUISE"]["GAS_RELEASED"] == 0

    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FR"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RR"],
    )
    ret.vEgoRaw = mean([ret.wheelSpeeds.fl, ret.wheelSpeeds.fr, ret.wheelSpeeds.rl, ret.wheelSpeeds.rr])
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    ret.standstill = ret.vEgoRaw < 0.001

    ret.steeringAngleDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]
    torque_sensor_angle_deg = cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]
    if self.dp_toyota_zss:
      torque_sensor_angle_deg = cp.vl["SECONDARY_STEER_ANGLE"]["ZORRO_STEER"]

    # On some cars, the angle measurement is non-zero while initializing
    if abs(torque_sensor_angle_deg) > 1e-3 and not bool(cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE_INITIALIZING"]):
      self.accurate_steer_angle_seen = True

    if self.accurate_steer_angle_seen:
      # Offset seems to be invalid for large steering angles
      if abs(ret.steeringAngleDeg) < 90 and cp.can_valid:
        self.angle_offset.update(torque_sensor_angle_deg - ret.steeringAngleDeg)

      if self.angle_offset.initialized:
        ret.steeringAngleOffsetDeg = self.angle_offset.x
        ret.steeringAngleDeg = torque_sensor_angle_deg - self.angle_offset.x

    ret.steeringRateDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_RATE"]

    can_gear = int(cp.vl["GEAR_PACKET"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))

    #dp: Thank you Arne (acceleration)
    if self.dp_toyota_ap_btn_link:
      if self.CP.carFingerprint in TSS2_CAR:
        sport_on = cp.vl["GEAR_PACKET"]['SPORT_ON']
        econ_on = cp.vl["GEAR_PACKET"]['ECON_ON']
      else:
        try:
          econ_on = cp.vl["GEAR_PACKET"]['ECON_ON']
        except KeyError:
          econ_on = 0
        if self.CP.carFingerprint == CAR.RAV4_TSS2:
          sport_on = cp.vl["GEAR_PACKET"]['SPORT_ON_2']
        else:
          try:
            sport_on = cp.vl["GEAR_PACKET"]['SPORT_ON']
          except KeyError:
            sport_on = 0
      if sport_on == 0 and econ_on == 0:
        self.dp_accel_profile = DP_ACCEL_NORMAL
      elif sport_on == 1:
        self.dp_accel_profile = DP_ACCEL_SPORT
      elif econ_on == 1:
        self.dp_accel_profile = DP_ACCEL_ECO

      # if init is false, we sync profile with whatever mode we have on car
      if not self.dp_accel_profile_init or self.dp_accel_profile != self.dp_accel_profile_prev:
        put_nonblocking('dp_accel_profile', str(self.dp_accel_profile))
        put_nonblocking('dp_last_modified',str(floor(time.time())))
        self.dp_accel_profile_init = True
      self.dp_accel_profile_prev = self.dp_accel_profile

    #dp: Thank you Arne (distance button)
    #if self.dp_toyota_fp_btn_link:
    #  if not self.read_distance_lines_init or self.read_distance_lines != cp.vl["PCM_CRUISE_SM"]['DISTANCE_LINES']:
    #    self.read_distance_lines_init = True
    #    self.read_distance_lines = cp.vl["PCM_CRUISE_SM"]['DISTANCE_LINES']
    #    put_nonblocking('dp_following_profile', str(int(max(self.read_distance_lines - 1, 0)))) # Skipping one profile.
    #    put_nonblocking('dp_last_modified',str(floor(time.time())))

    #if self.dp_toyota_lkas_btn_link:
    #  if not self.read_lkas_btn_init or self.read_lkas_btn != cp_cam.vl["LKAS_HUD"]['SET_ME_X01']:
    #    self.read_lkas_btn_init = True
    #    self.read_lkas_btn = cp_cam.vl["LKAS_HUD"]['SET_ME_X01']
    #    put_nonblocking('dp_lane_less_mode', str(int(max(self.read_lkas_btn, 0)))) #dlp = lane, e2e.
    #    put_nonblocking('dp_last_modified',str(floor(time.time())))

    #dp
    ret.engineRPM = cp.vl["ENGINE_RPM"]['RPM']
    ret.leftBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 1
    ret.rightBlinker = cp.vl["BLINKERS_STATE"]["TURN_SIGNALS"] == 2

    ret.steeringTorque = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_DRIVER"]
    ret.steeringTorqueEps = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_EPS"] * self.eps_torque_scale
    # we could use the override bit from dbc, but it's triggered at too high torque values
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD
    ret.steerFaultTemporary = cp.vl["EPS_STATUS"]["LKA_STATE"] not in (1, 5)

    if self.CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC, CAR.LEXUS_ISH, CAR.LEXUS_GSH, CAR.LEXUS_NXT):
      ret.cruiseState.available = cp.vl["DSU_CRUISE"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["DSU_CRUISE"]["SET_SPEED"] * CV.KPH_TO_MS
    else:
      ret.cruiseState.available = cp.vl["PCM_CRUISE_2"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["PCM_CRUISE_2"]["SET_SPEED"] * CV.KPH_TO_MS

    if self.CP.carFingerprint in RADAR_ACC_CAR:
      self.acc_type = cp.vl["ACC_CONTROL"]["ACC_TYPE"]
      ret.stockFcw = bool(cp.vl["ACC_HUD"]["FCW"])
    elif self.CP.carFingerprint in TSS2_CAR:
      self.acc_type = cp_cam.vl["ACC_CONTROL"]["ACC_TYPE"]
      ret.stockFcw = bool(cp_cam.vl["ACC_HUD"]["FCW"])

    # some TSS2 cars have low speed lockout permanently set, so ignore on those cars
    # these cars are identified by an ACC_TYPE value of 2.
    # TODO: it is possible to avoid the lockout and gain stop and go if you
    # send your own ACC_CONTROL msg on startup with ACC_TYPE set to 1
    if (self.CP.carFingerprint not in TSS2_CAR and self.CP.carFingerprint not in (CAR.LEXUS_IS, CAR.LEXUS_RC, CAR.LEXUS_ISH, CAR.LEXUS_GSH)) or \
       (self.CP.carFingerprint in TSS2_CAR and self.acc_type == 1):
      self.low_speed_lockout = cp.vl["PCM_CRUISE_2"]["LOW_SPEED_LOCKOUT"] == 2

    self.pcm_acc_status = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint in NO_STOP_TIMER_CAR or self.CP.enableGasInterceptor:
      # ignore standstill in hybrid vehicles, since pcm allows to restart without
      # receiving any special command. Also if interceptor is detected
      ret.cruiseState.standstill = False
    else:
      ret.cruiseState.standstill = self.pcm_acc_status == 7
    ret.cruiseState.enabled = bool(cp.vl["PCM_CRUISE"]["CRUISE_ACTIVE"])
    # dp
    ret.cruiseActualEnabled = ret.cruiseState.enabled
    ret.cruiseState.nonAdaptive = cp.vl["PCM_CRUISE"]["CRUISE_STATE"] in (1, 2, 3, 4, 5, 6)

    ret.genericToggle = bool(cp.vl["LIGHT_STALK"]["AUTO_HIGH_BEAM"])
    ret.stockAeb = bool(cp_cam.vl["PRE_COLLISION"]["PRECOLLISION_ACTIVE"] and cp_cam.vl["PRE_COLLISION"]["FORCE"] < -1e-5)

    ret.espDisabled = cp.vl["ESP_CONTROL"]["TC_DISABLED"] != 0
    # 2 is standby, 10 is active. TODO: check that everything else is really a faulty state
    self.steer_state = cp.vl["EPS_STATUS"]["LKA_STATE"]

    if self.CP.enableBsm:
      ret.leftBlindspot = (cp.vl["BSM"]["L_ADJACENT"] == 1) or (cp.vl["BSM"]["L_APPROACHING"] == 1)
      ret.rightBlindspot = (cp.vl["BSM"]["R_ADJACENT"] == 1) or (cp.vl["BSM"]["R_APPROACHING"] == 1)

    # dp
    # distance button
    self.distance = 1 if cp_cam.vl["ACC_CONTROL"]["DISTANCE"] == 1 else 0
    ret.distanceLines = cp.vl["PCM_CRUISE_SM"]["DISTANCE_LINES"

    return ret

  @staticmethod
  def get_can_parser(CP):
    signals = [
      # sig_name, sig_address
      ("STEER_ANGLE", "STEER_ANGLE_SENSOR"),
      ("GEAR", "GEAR_PACKET"),
      ("BRAKE_PRESSED", "BRAKE_MODULE"),
      ("WHEEL_SPEED_FL", "WHEEL_SPEEDS"),
      ("WHEEL_SPEED_FR", "WHEEL_SPEEDS"),
      ("WHEEL_SPEED_RL", "WHEEL_SPEEDS"),
      ("WHEEL_SPEED_RR", "WHEEL_SPEEDS"),
      ("DOOR_OPEN_FL", "BODY_CONTROL_STATE"),
      ("DOOR_OPEN_FR", "BODY_CONTROL_STATE"),
      ("DOOR_OPEN_RL", "BODY_CONTROL_STATE"),
      ("DOOR_OPEN_RR", "BODY_CONTROL_STATE"),
      ("SEATBELT_DRIVER_UNLATCHED", "BODY_CONTROL_STATE"),
      ("PARKING_BRAKE", "BODY_CONTROL_STATE"),
      ("TC_DISABLED", "ESP_CONTROL"),
      #("BRAKE_HOLD_ACTIVE", "ESP_CONTROL"),
      ("STEER_FRACTION", "STEER_ANGLE_SENSOR"),
      ("STEER_RATE", "STEER_ANGLE_SENSOR"),
      ("CRUISE_ACTIVE", "PCM_CRUISE"),
      ("CRUISE_STATE", "PCM_CRUISE"),
      ("GAS_RELEASED", "PCM_CRUISE"),
      ("STEER_TORQUE_DRIVER", "STEER_TORQUE_SENSOR"),
      ("STEER_TORQUE_EPS", "STEER_TORQUE_SENSOR"),
      ("STEER_ANGLE", "STEER_TORQUE_SENSOR"),
      ("STEER_ANGLE_INITIALIZING", "STEER_TORQUE_SENSOR"),
      ("TURN_SIGNALS", "BLINKERS_STATE"),
      ("LKA_STATE", "EPS_STATUS"),
      ("AUTO_HIGH_BEAM", "LIGHT_STALK"),
      #dp
      ("SPORT_ON", "GEAR_PACKET"),
      ("ECON_ON", "GEAR_PACKET"),
      ("DISTANCE_LINES", "PCM_CRUISE_SM"),
      ("RPM", "ENGINE_RPM"),
      ("BRAKE_LIGHTS_ACC", "ESP_CONTROL"),
    ]

    checks = [
      ("GEAR_PACKET", 1),
      ("LIGHT_STALK", 1),
      ("BLINKERS_STATE", 0.15),
      ("BODY_CONTROL_STATE", 3),
      ("ESP_CONTROL", 3),
      ("EPS_STATUS", 25),
      ("BRAKE_MODULE", 40),
      ("WHEEL_SPEEDS", 80),
      ("STEER_ANGLE_SENSOR", 80),
      ("PCM_CRUISE", 33),
      ("STEER_TORQUE_SENSOR", 50),
      #dp
      ("ENGINE_RPM", 100),
      ("PCM_CRUISE_SM", 1),
    ]

    if CP.flags & ToyotaFlags.HYBRID:
      signals.append(("GAS_PEDAL", "GAS_PEDAL_HYBRID"))
      checks.append(("GAS_PEDAL_HYBRID", 33))
    else:
      signals.append(("GAS_PEDAL", "GAS_PEDAL"))
      checks.append(("GAS_PEDAL", 33))

    #dp acceleration
    if CP.carFingerprint == CAR.RAV4_TSS2:
      signals.append(("SPORT_ON_2", "GEAR_PACKET"))

    if CP.carFingerprint in (CAR.LEXUS_ESH_TSS2, CAR.RAV4H_TSS2, CAR.CHRH, CAR.PRIUS_TSS2, CAR.HIGHLANDERH_TSS2):
      signals.append(("SPORT_ON", "GEAR_PACKET2"))
      signals.append(("ECON_ON", "GEAR_PACKET2"))

    if CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC, CAR.LEXUS_ISH, CAR.LEXUS_GSH, CAR.LEXUS_NXT):
      signals.append(("MAIN_ON", "DSU_CRUISE"))
      signals.append(("SET_SPEED", "DSU_CRUISE"))
      signals.append(("MAIN_ON", "DSU_CRUISE"))
      signals.append(("SET_SPEED", "DSU_CRUISE"))
      checks.append(("DSU_CRUISE", 5))
    else:
      signals.append(("MAIN_ON", "PCM_CRUISE_2"))
      signals.append(("SET_SPEED", "PCM_CRUISE_2"))
      signals.append(("LOW_SPEED_LOCKOUT", "PCM_CRUISE_2"))
      checks.append(("PCM_CRUISE_2", 33))

    if CP.carFingerprint in (CAR.LEXUS_ISH, CAR.LEXUS_GSH):
      signals.append(("GAS_PEDAL", "GAS_PEDAL_ALT"))
      checks.append(("GAS_PEDAL_ALT", 33))
    else:
      signals.append(("GAS_PEDAL", "GAS_PEDAL"))
      checks.append(("GAS_PEDAL", 33))

    # add gas interceptor reading if we are using it
    if CP.enableGasInterceptor:
      signals.append(("INTERCEPTOR_GAS", "GAS_SENSOR"))
      signals.append(("INTERCEPTOR_GAS2", "GAS_SENSOR"))
      checks.append(("GAS_SENSOR", 50))

    if CP.enableBsm:
      signals += [
        ("L_ADJACENT", "BSM"),
        ("L_APPROACHING", "BSM"),
        ("R_ADJACENT", "BSM"),
        ("R_APPROACHING", "BSM"),
      ]
      checks.append(("BSM", 1))

    if Params().get('dp_toyota_zss') == b'1':
      signals += [("ZORRO_STEER", "SECONDARY_STEER_ANGLE")]

    if CP.carFingerprint in RADAR_ACC_CAR:
      signals += [
        ("ACC_TYPE", "ACC_CONTROL"),
        ("FCW", "ACC_HUD"),
      ]
      checks += [
        ("ACC_CONTROL", 33),
        ("ACC_HUD", 1),
      ]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    signals = [
      ("FORCE", "PRE_COLLISION"),
      ("PRECOLLISION_ACTIVE", "PRE_COLLISION"),
      #dp
      ("DISTANCE", "ACC_CONTROL"),
      #("SET_ME_X01", "LKAS_HUD"),
    ]

    # use steering message to check if panda is connected to frc
    checks = [
      ("STEERING_LKA", 42),
      ("PRE_COLLISION", 0), # TODO: figure out why freq is inconsistent
    ]

    if CP.carFingerprint in (TSS2_CAR - RADAR_ACC_CAR):
      signals += [
        ("ACC_TYPE", "ACC_CONTROL"),
        ("FCW", "ACC_HUD"),
      ]
      checks += [
        ("ACC_CONTROL", 33),
        ("ACC_HUD", 1),
      ]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2)
