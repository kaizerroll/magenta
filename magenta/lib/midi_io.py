# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MIDI ops.

Input and output wrappers for converting between MIDI and other formats.
"""

from collections import defaultdict
from cStringIO import StringIO
import sys

import pretty_midi
import tensorflow as tf

from magenta.protobuf import music_pb2


# The offset used to change the mode of a key from major to minor when
# generating a PrettyMIDI KeySignature.
_PRETTY_MIDI_MAJOR_TO_MINOR_OFFSET = 12


class MIDIConversionError(Exception):
  pass


def midi_to_sequence_proto(midi_data, continue_on_exception=False):
  """Convert MIDI file contents to a tensorflow.magenta.NoteSequence proto.

  Converts a MIDI file encoded as a string into a
  tensorflow.magenta.NoteSequence proto. Decoding errors are very common when
  working with large sets of MIDI files. To support batch processing the
  argument continue_on_exception (when True) will catch all exceptions from the
  decoding library pretty_midi, log an error, and return None.

  Args:
    midi_data: A string contaning the contents of a MIDI file or populated
      pretty_midi.PrettyMIDI object.
    continue_on_exception: A boolean that when true causes all exceptions from
      the decoder to be caught and ignored. Instead, an error is logged and None
      is returned.

  Returns:
    A tensorflow.magenta.NoteSequence proto or None if midi_data could not be
    decoded and if continue_on_exception is True.

  Raises:
    MIDIConversionError: An improper MIDI mode was supplied.
  """

  # In practice many MIDI files cannot be decoded with pretty_midi. Catch all
  # errors here and try to log a meaningful message. So many different
  # exceptions are raised in pretty_midi.PrettyMidi that it is cumbersome to
  # catch them all only for the purpose of error logging.
  # pylint: disable=bare-except
  if isinstance(midi_data, pretty_midi.PrettyMIDI):
    midi = midi_data
  else:
    try:
      midi = pretty_midi.PrettyMIDI(StringIO(midi_data))
    except:
      if continue_on_exception:
        tf.logging.error('Midi decoding error %s: %s', sys.exc_info()[0],
                         sys.exc_info()[1])
        return None
      else:
        raise MIDIConversionError('Midi decoding error %s: %s',
                                  sys.exc_info()[0], sys.exc_info()[1])
  # pylint: enable=bare-except

  sequence = music_pb2.NoteSequence()

  # Populate header.
  sequence.ticks_per_beat = midi.resolution

  # Populate time signatures.
  for midi_time in midi.time_signature_changes:
    time_signature = sequence.time_signatures.add()
    time_signature.time = midi_time.time
    time_signature.numerator = midi_time.numerator
    time_signature.denominator = midi_time.denominator

  # Populate key signatures.
  for midi_key in midi.key_signature_changes:
    key_signature = sequence.key_signatures.add()
    key_signature.time = midi_key.time
    key_signature.key = midi_key.key_number % 12
    midi_mode = midi_key.key_number / 12
    if midi_mode == 0:
      key_signature.mode = key_signature.MAJOR
    elif midi_mode == 1:
      key_signature.mode = key_signature.MINOR
    else:
      raise MIDIConversionError('Invalid midi_mode %i' % midi_mode)

  # Populate tempo changes.
  tempo_times, tempo_bpms = midi.get_tempo_changes()
  for time_in_seconds, tempo_in_bpm in zip(tempo_times, tempo_bpms):
    tempo = sequence.tempos.add()
    tempo.time = time_in_seconds
    tempo.bpm = tempo_in_bpm

  # Populate notes by first gathering them all from the midi's instruments, then
  # sorting them primarily by start and secondarily by end, and finally looping
  # through this sorted list and appending each as a new sequence.note. We also
  # here set the sequence.total_time as the max end time in the notes.
  # TODO(@douglaseck): Eliminate some of this boilerplate code.
  midi_notes = []
  midi_pitch_bends = []
  midi_control_changes = []
  for num_instrument, midi_instrument in enumerate(midi.instruments):
    for midi_note in midi_instrument.notes:
      if not sequence.total_time or midi_note.end > sequence.total_time:
        sequence.total_time = midi_note.end
      midi_notes.append((midi_instrument.program, num_instrument, midi_note))
    for midi_pitch_bend in midi_instrument.pitch_bends:
      midi_pitch_bends.append(
          (midi_instrument.program, num_instrument, midi_pitch_bend))
    for midi_control_change in midi_instrument.control_changes:
      midi_control_changes.append(
          (midi_instrument.program, num_instrument, midi_control_change))

  for program, instrument, midi_note in midi_notes:
    note = sequence.notes.add()
    note.instrument = instrument
    note.program = program
    note.start_time = midi_note.start
    note.end_time = midi_note.end
    note.pitch = midi_note.pitch
    note.velocity = midi_note.velocity

  for program, instrument, midi_pitch_bend in midi_pitch_bends:
    pitch_bend = sequence.pitch_bends.add()
    pitch_bend.instrument = instrument
    pitch_bend.program = program
    pitch_bend.time = midi_pitch_bend.time
    pitch_bend.bend = midi_pitch_bend.pitch

  for program, instrument, midi_control_change in midi_control_changes:
    control_change = sequence.control_changes.add()
    control_change.instrument = instrument
    control_change.program = program
    control_change.time = midi_control_change.time
    control_change.control_number = midi_control_change.number
    control_change.control_value = midi_control_change.value

  # TODO(@douglaseck): Estimate note type (e.g. quarter note) and populate
  # note.numerator and note.denominator.

  return sequence


def sequence_proto_to_pretty_midi(sequence):
  """Convert tensorflow.magenta.NoteSequence proto to a PrettyMIDI.

  Time is stored in the NoteSequence in absolute values (seconds) as opposed to
  relative values (MIDI ticks). When the NoteSequence is translated back to
  PrettyMIDI the absolute time is retained. The tempo map is also recreated.

  Args:
    sequence: A tensorfow.magenta.NoteSequence proto.

  Returns:
    A pretty_midi.PrettyMIDI object or None if sequence could not be decoded.
  """

  kwargs = {}
  if sequence.tempos and sequence.tempos[0].time == 0:
    kwargs['initial_tempo'] = sequence.tempos[0].bpm
  pm = pretty_midi.PrettyMIDI(resolution=sequence.ticks_per_beat, **kwargs)

  # Create an empty instrument to contain time and key signatures.
  instrument = pretty_midi.Instrument(0)
  pm.instruments.append(instrument)

  # Populate time signatures.
  for seq_ts in sequence.time_signatures:
    time_signature = pretty_midi.containers.TimeSignature(
        seq_ts.numerator, seq_ts.denominator, seq_ts.time)
    pm.time_signature_changes.append(time_signature)

  # Populate key signatures.
  for seq_key in sequence.key_signatures:
    key_number = seq_key.key
    if seq_key.mode == seq_key.MINOR:
      key_number += _PRETTY_MIDI_MAJOR_TO_MINOR_OFFSET
    key_signature = pretty_midi.containers.KeySignature(
        key_number, seq_key.time)
    pm.key_signature_changes.append(key_signature)

  # Populate tempo. The first tempo change was done in PrettyMIDI constructor.
  # TODO(@douglaseck): Update this code if pretty_midi adds the ability to
  # write tempo.
  if len(sequence.tempos) > 1:
    for seq_tempo in sequence.tempos[1:]:
      tick_scale = 60.0 / (pm.resolution * seq_tempo.bpm)
      tick = pm.time_to_tick(seq_tempo.time)
      # pylint: disable=protected-access
      pm._PrettyMIDI__tick_scales.append((tick, tick_scale))
      # pylint: enable=protected-access

  # Populate instrument events by first gathering notes and other event types
  # in lists then write them sorted to the PrettyMidi object.
  instrument_events = defaultdict(lambda: defaultdict(list))
  for seq_note in sequence.notes:
    instrument_events[(seq_note.instrument, seq_note.program)]['notes'].append(
        pretty_midi.Note(seq_note.velocity, seq_note.pitch,
                         seq_note.start_time, seq_note.end_time))
  for seq_bend in sequence.pitch_bends:
    instrument_events[(seq_bend.instrument, seq_bend.program)]['bends'].append(
        pretty_midi.PitchBend(seq_bend.bend, seq_bend.time))
  for seq_cc in sequence.control_changes:
    instrument_events[(seq_cc.instrument, seq_cc.program)]['controls'].append(
        pretty_midi.ControlChange(seq_cc.control_number,
                                  seq_cc.control_value, seq_cc.time))

  for (instr_id, prog_id) in sorted(instrument_events.keys()):
    # For instr_id 0 append to the instrument created above.
    if instr_id > 0:
      instrument = pretty_midi.Instrument(prog_id, is_drum=(instr_id == 9))
      pm.instruments.append(instrument)
    instrument.program = prog_id
    instrument.notes = instrument_events[(instr_id, prog_id)]['notes']
    instrument.pitch_bends = instrument_events[(instr_id, prog_id)]['bends']
    instrument.control_changes = instrument_events[
        (instr_id, prog_id)]['controls']

  return pm


def midi_file_to_sequence_proto(midi_file, continue_on_exception=False):
  """Converts MIDI file to a tensorflow.magenta.NoteSequence proto.

  Args:
    midi_file: A string path to a MIDI file.
    continue_on_exception: A boolean decreeing if we should continue on MIDI to
        sequence conversion exception.

  Returns:
    A tensorflow.magenta.Sequence proto or None if the MIDI could not be decoded
    and continue_on_exception is True.

  Raises:
    MIDIConversionError: Invalid midi_file and continue_on_exception is False.
  """
  with tf.gfile.Open(midi_file, 'r') as f:
    midi_as_string = f.read()
    try:
      return midi_to_sequence_proto(midi_as_string)
    except MIDIConversionError, e:
      if continue_on_exception:
        return None
      else:
        raise MIDIConversionError(e)


def sequence_proto_to_midi_file(sequence, output_file):
  """Convert tensorflow.magenta.NoteSequence proto to a MIDI file on disk.

  Time is stored in the NoteSequence in absolute values (seconds) as opposed to
  relative values (MIDI ticks). When the NoteSequence is translated back to
  MIDI the absolute time is retained. The tempo map is also recreated.

  Args:
    sequence: A tensorfow.magenta.NoteSequence proto.
    output_file: String path to MIDI file that will be written.
  """
  pretty_midi_object = sequence_proto_to_pretty_midi(sequence)
  pretty_midi_object.write(output_file)
