# Copyright 2013 Google Inc. All Rights Reserved.
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

"""A module that provides parsing utilities for argparse.

For details of how argparse argument pasers work, see:

  http://docs.python.org/dev/library/argparse.html#type

Example usage:

  import argparse
  import arg_parsers

  parser = argparse.ArgumentParser()

  parser.add_argument(
      '--metadata',
      type=arg_parsers.ArgDict())
  parser.add_argument(
      '--delay',
      default='5s',
      type=arg_parsers.Duration(lower_bound='1s', upper_bound='10s')
  parser.add_argument(
      '--disk-size',
      default='10GB',
      type=arg_parsers.BinarySize(lower_bound='1GB', upper_bound='10TB')

  res = parser.parse_args(
      '--names --metadata x=y,a=b,c=d --delay 1s --disk-size 10gb'.split())

  assert res.metadata == {'a': 'b', 'c': 'd', 'x': 'y'}
  assert res.delay == 1
  assert res.disk_size == 10737418240

"""

import argparse
import copy
import re

from googlecloudsdk.calliope import parser_errors
from googlecloudsdk.core import log
from googlecloudsdk.core.util import times


__all__ = ['Duration', 'BinarySize']


class Error(Exception):
  """Exceptions that are defined by this module."""


class ArgumentTypeError(Error, argparse.ArgumentTypeError):
  """Exceptions for parsers that are used as argparse types."""


class ArgumentParsingError(Error, argparse.ArgumentError):
  """Raised when there is a problem with user input.

  argparse.ArgumentError takes both the action and a message as constructor
  parameters.
  """


def _GenerateErrorMessage(error, user_input=None, error_idx=None):
  """Constructs an error message for an exception.

  Args:
    error: str, The error message that should be displayed. This
      message should not end with any punctuation--the full error
      message is constructed by appending more information to error.
    user_input: str, The user input that caused the error.
    error_idx: int, The index at which the error occurred. If None,
      the index will not be printed in the error message.

  Returns:
    str: The message to use for the exception.
  """
  if user_input is None:
    return error
  elif not user_input:  # Is input empty?
    return error + '; received empty string'
  elif error_idx is None:
    return error + '; received: ' + user_input
  return ('{error_message} at index {error_idx}: {user_input}'
          .format(error_message=error, user_input=user_input,
                  error_idx=error_idx))


_VALUE_PATTERN = r"""
    ^                       # Beginning of input marker.
    (?P<amount>\d+)         # Amount.
    ((?P<unit>[a-zA-Z]+))?  # Optional unit.
    $                       # End of input marker.
"""

_RANGE_PATTERN = r'^(?P<start>[0-9]+)(-(?P<end>[0-9]+))?$'

_SECOND = 1
_MINUTE = 60 * _SECOND
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

# The units are adopted from sleep(1):
#   http://linux.die.net/man/1/sleep
_DURATION_SCALES = {
    's': _SECOND,
    'm': _MINUTE,
    'h': _HOUR,
    'd': _DAY,
}

_BINARY_SIZE_SCALES = {
    'B': 1,
    'KB': 1 << 10,
    'MB': 1 << 20,
    'GB': 1 << 30,
    'TB': 1 << 40,
    'PB': 1 << 50,
    'KiB': 1 << 10,
    'MiB': 1 << 20,
    'GiB': 1 << 30,
    'TiB': 1 << 40,
    'PiB': 1 << 50,
}


def GetMultiCompleter(individual_completer):
  """Create a completer to handle completion for comma separated lists.

  Args:
    individual_completer: A function that completes an individual element.

  Returns:
    A function that completes the last element of the list.
  """
  def MultiCompleter(prefix, parsed_args, **kwargs):
    start = ''
    lst = prefix.rsplit(',', 1)
    if len(lst) > 1:
      start = lst[0] + ','
      prefix = lst[1]
    matches = individual_completer(prefix, parsed_args, **kwargs)
    return [start + match for match in matches]
  return MultiCompleter


def _ValueParser(scales, default_unit, lower_bound=None, upper_bound=None,
                 strict_case=True, suggested_binary_size_scales=None):
  """A helper that returns a function that can parse values with units.

  Casing for all units matters.

  Args:
    scales: {str: int}, A dictionary mapping units to their magnitudes in
      relation to the lowest magnitude unit in the dict.
    default_unit: str, The default unit to use if the user's input is
      missing unit.
    lower_bound: str, An inclusive lower bound.
    upper_bound: str, An inclusive upper bound.
    strict_case: bool, whether to be strict on case-checking
    suggested_binary_size_scales: list, A list of strings with units that will
                                    be recommended to user.

  Returns:
    A function that can parse values.
  """

  def UnitsByMagnitude(suggested_binary_size_scales=None):
    """Returns a list of the units in scales sorted by magnitude."""
    scale_items = sorted(scales.iteritems(),
                         key=lambda value: (value[1], value[0]))
    if suggested_binary_size_scales is None:
      return [key for key, _ in scale_items]
    return [key for key, _ in scale_items
            if key in suggested_binary_size_scales]

  def Parse(value):
    """Parses value that can contain a unit."""
    match = re.match(_VALUE_PATTERN, value, re.VERBOSE)
    if not match:
      raise ArgumentTypeError(_GenerateErrorMessage(
          'given value must be of the form INTEGER[UNIT] where units '
          'can be one of {0}'
          .format(', '.join(UnitsByMagnitude(suggested_binary_size_scales))),
          user_input=value))

    amount = int(match.group('amount'))
    unit = match.group('unit')
    if strict_case:
      unit_case = unit
      default_unit_case = default_unit
      scales_case = scales
    else:
      unit_case = unit and unit.upper()
      default_unit_case = default_unit.upper()
      scales_case = dict([(k.upper(), v) for k, v in scales.items()])

    if unit_case is None:
      return amount * scales_case[default_unit_case]
    elif unit_case in scales_case:
      return amount * scales_case[unit_case]
    else:
      raise ArgumentTypeError(_GenerateErrorMessage(
          'unit must be one of {0}'.format(', '.join(UnitsByMagnitude())),
          user_input=unit))

  if lower_bound is None:
    parsed_lower_bound = None
  else:
    parsed_lower_bound = Parse(lower_bound)

  if upper_bound is None:
    parsed_upper_bound = None
  else:
    parsed_upper_bound = Parse(upper_bound)

  def ParseWithBoundsChecking(value):
    """Same as Parse except bound checking is performed."""
    if value is None:
      return None
    else:
      parsed_value = Parse(value)
      if parsed_lower_bound is not None and parsed_value < parsed_lower_bound:
        raise ArgumentTypeError(_GenerateErrorMessage(
            'value must be greater than or equal to {0}'.format(lower_bound),
            user_input=value))
      elif parsed_upper_bound is not None and parsed_value > parsed_upper_bound:
        raise ArgumentTypeError(_GenerateErrorMessage(
            'value must be less than or equal to {0}'.format(upper_bound),
            user_input=value))
      else:
        return parsed_value

  return ParseWithBoundsChecking


def RegexpValidator(pattern, description):
  """Returns a function that validates a string against a regular expression.

  For example:

  >>> alphanumeric_type = RegexpValidator(
  ...   r'[a-zA-Z0-9]+',
  ...   'must contain one or more alphanumeric characters')
  >>> parser.add_argument('--foo', type=alphanumeric_type)
  >>> parser.parse_args(['--foo', '?'])
  >>> # SystemExit raised and the error "error: argument foo: Bad value [?]:
  >>> # must contain one or more alphanumeric characters" is displayed

  Args:
    pattern: str, the pattern to compile into a regular expression to check
    description: an error message to show if the argument doesn't match

  Returns:
    function: str -> str, usable as an argparse type
  """
  def Parse(value):
    if not re.match(pattern + '$', value):
      raise ArgumentTypeError('Bad value [{0}]: {1}'.format(value, description))
    return value
  return Parse


def Duration(lower_bound=None, upper_bound=None):
  """Returns a function that can parse time durations.

  Input to the parsing function must be a string of the form:

    INTEGER[UNIT]

  The integer must be non-negative. Valid units are "s", "m", "h", and
  "d" for seconds, seconds, minutes, hours, and days,
  respectively. The casing of the units matters.

  If the unit is omitted, seconds is assumed.

  The result is parsed in seconds. For example:

    parser = Duration()
    assert parser('10s') == 10

  Args:
    lower_bound: str, An inclusive lower bound for values.
    upper_bound: str, An inclusive upper bound for values.

  Raises:
    ArgumentTypeError: If either the lower_bound or upper_bound
      cannot be parsed. The returned function will also raise this
      error if it cannot parse its input. This exception is also
      raised if the returned function receives an out-of-bounds
      input.

  Returns:
    A function that accepts a single time duration as input to be
      parsed.
  """
  return _ValueParser(_DURATION_SCALES, default_unit='s',
                      lower_bound=lower_bound, upper_bound=upper_bound)


def BinarySize(lower_bound=None, upper_bound=None,
               suggested_binary_size_scales=None, default_unit='GB'):
  """Returns a function that can parse binary sizes.

  Binary sizes are defined as base-2 values representing number of
  bytes.

  Input to the parsing function must be a string of the form:

    INTEGER[UNIT]

  The integer must be non-negative. Valid units are "B", "KB", "MB",
  "GB", "TB", "KiB", "MiB", "GiB", "TiB", "PiB".  If the unit is
  omitted, GB is assumed.

  The result is parsed in bytes. For example:

    parser = BinarySize()
    assert parser('10GB') == 1073741824

  Args:
    lower_bound: str, An inclusive lower bound for values.
    upper_bound: str, An inclusive upper bound for values.
    suggested_binary_size_scales: list, A list of strings with units that will
                                    be recommended to user.
    default_unit: str, unit used when user did not specify unit.

  Raises:
    ArgumentTypeError: If either the lower_bound or upper_bound
      cannot be parsed. The returned function will also raise this
      error if it cannot parse its input. This exception is also
      raised if the returned function receives an out-of-bounds
      input.

  Returns:
    A function that accepts a single binary size as input to be
      parsed.
  """
  return _ValueParser(
      _BINARY_SIZE_SCALES, default_unit=default_unit,
      lower_bound=lower_bound, upper_bound=upper_bound,
      strict_case=False,
      suggested_binary_size_scales=suggested_binary_size_scales)


_KV_PAIR_DELIMITER = '='


class Range(object):
  """Range of integer values."""

  def __init__(self, start, end):
    self.start = start
    self.end = end

  @staticmethod
  def Parse(string_value):
    """Creates Range object out of given string value."""
    match = re.match(_RANGE_PATTERN, string_value)
    if not match:
      raise ArgumentTypeError('Expected a non-negative integer value or a '
                              'range of such values instead of "{0}"'
                              .format(string_value))
    start = int(match.group('start'))
    end = match.group('end')
    if end is None:
      end = start
    else:
      end = int(end)
    if end < start:
      raise ArgumentTypeError('Expected range start {0} smaller or equal to '
                              'range end {1} in "{2}"'.format(
                                  start, end, string_value))
    return Range(start, end)

  def Combine(self, other):
    """Combines two overlapping or adjacent ranges, raises otherwise."""
    if self.end + 1 < other.start or self.start > other.end + 1:
      raise Error('Cannot combine non-overlapping or non-adjacent ranges '
                  '{0} and {1}'.format(self, other))
    return Range(min(self.start, other.start), max(self.end, other.end))

  def __eq__(self, other):
    if isinstance(other, Range):
      return self.start == other.start and self.end == other.end
    return False

  def __lt__(self, other):
    if self.start == other.start:
      return self.end < other.end
    return self.start < other.start

  def __str__(self):
    if self.start == self.end:
      return str(self.start)
    return '{0}-{1}'.format(self.start, self.end)


class HostPort(object):
  """A class for holding host and port information."""

  IPV4_OR_HOST_PATTERN = r'^(?P<address>[\w\d\.-]+)?(:|:(?P<port>[\d]+))?$'
  # includes hostnames
  IPV6_PATTERN = r'^(\[(?P<address>[\w\d:]+)\])(:|:(?P<port>[\d]+))?$'

  def __init__(self, host, port):
    self.host = host
    self.port = port

  @staticmethod
  def Parse(s, ipv6_enabled=False):
    """Parse the given string into a HostPort object.

    This can be used as an argparse type.

    Args:
      s: str, The string to parse. If ipv6_enabled and host is an IPv6 address,
      it should be placed in square brackets: e.g.
        [2001:db8:0:0:0:ff00:42:8329]
        or
        [2001:db8:0:0:0:ff00:42:8329]:8080
      ipv6_enabled: boolean, If True then accept IPv6 addresses.

    Raises:
      ArgumentTypeError: If the string is not valid.

    Returns:
      HostPort, The parsed object.
    """
    if not s:
      return HostPort(None, None)

    match = re.match(HostPort.IPV4_OR_HOST_PATTERN, s, re.UNICODE)
    if ipv6_enabled and not match:
      match = re.match(HostPort.IPV6_PATTERN, s, re.UNICODE)
      if not match:
        raise ArgumentTypeError(_GenerateErrorMessage(
            'Failed to parse host and port. Expected format \n\n'
            '  IPv4_ADDRESS_OR_HOSTNAME:PORT\n\n'
            'or\n\n'
            '  [IPv6_ADDRESS]:PORT\n\n'
            '(where :PORT is optional).',
            user_input=s))
    elif not match:
      raise ArgumentTypeError(_GenerateErrorMessage(
          'Failed to parse host and port. Expected format \n\n'
          '  IPv4_ADDRESS_OR_HOSTNAME:PORT\n\n'
          '(where :PORT is optional).',
          user_input=s))
    return HostPort(match.group('address'), match.group('port'))


class Day(object):
  """A class for parsing a datetime object for a specific day."""

  @staticmethod
  def Parse(s):
    if not s:
      return None
    try:
      return times.ParseDateTime(s, '%Y-%m-%d').date()
    except times.Error as e:
      raise ArgumentTypeError(
          _GenerateErrorMessage(u'Failed to parse date: {0}'.format(unicode(e)),
                                user_input=s))


class Datetime(object):
  """A class for parsing a datetime object in UTC timezone."""

  @staticmethod
  def Parse(s):
    """Parses a string value into a Datetime object."""
    if not s:
      return None
    try:
      return times.ParseDateTime(s)
    except times.Error as e:
      raise ArgumentTypeError(
          _GenerateErrorMessage(
              u'Failed to parse date/time: {0}'.format(unicode(e)),
              user_input=s))


class DayOfWeek(object):
  """A class for parsing a day of the week."""

  DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT']

  @staticmethod
  def Parse(s):
    """Validates and normalizes a string as a day of the week."""
    if not s:
      return None
    fixed = s.upper()[:3]
    if fixed not in DayOfWeek.DAYS:
      raise ArgumentTypeError(
          _GenerateErrorMessage(
              'Failed to parse day of week. Value should be one of {0}'.format(
                  ', '.join(DayOfWeek.DAYS)),
              user_input=s))
    return fixed


def _BoundedType(type_builder, type_description,
                 lower_bound=None, upper_bound=None, unlimited=False):
  """Returns a function that can parse given type within some bound.

  Args:
    type_builder: A callable for building the requested type from the value
        string.
    type_description: str, Description of the requested type (for verbose
        messages).
    lower_bound: of type compatible with type_builder,
        The value must be >= lower_bound.
    upper_bound: of type compatible with type_builder,
        The value must be <= upper_bound.
    unlimited: bool, If True then a value of 'unlimited' means no limit.

  Returns:
    A function that can parse given type within some bound.
  """

  def Parse(value):
    """Parses value as a type constructed by type_builder.

    Args:
      value: str, Value to be converted to the requested type.

    Raises:
      ArgumentTypeError: If the provided value is out of bounds or unparsable.

    Returns:
      Value converted to the requested type.
    """
    if unlimited and value == 'unlimited':
      return None

    try:
      v = type_builder(value)
    except ValueError:
      raise ArgumentTypeError(
          _GenerateErrorMessage('Value must be {0}'.format(type_description),
                                user_input=value))

    if lower_bound is not None and v < lower_bound:
      raise ArgumentTypeError(
          _GenerateErrorMessage(
              'Value must be greater than or equal to {0}'.format(lower_bound),
              user_input=value))

    if upper_bound is not None and upper_bound < v:
      raise ArgumentTypeError(
          _GenerateErrorMessage(
              'Value must be less than or equal to {0}'.format(upper_bound),
              user_input=value))

    return v

  return Parse


def BoundedInt(*args, **kwargs):
  return _BoundedType(int, 'an integer', *args, **kwargs)


def BoundedFloat(*args, **kwargs):
  return _BoundedType(float, 'a floating point number', *args, **kwargs)


def _TokenizeQuotedList(arg_value, delim=','):
  """Tokenize an argument into a list.

  Args:
    arg_value: str, The raw argument.
    delim: str, The delimiter on which to split the argument string.

  Returns:
    [str], The tokenized list.
  """
  if arg_value:
    if not arg_value.endswith(delim):
      arg_value += delim
    return arg_value.split(delim)[:-1]
  return []


class ArgType(object):
  """Base class for arg types."""


class ArgList(ArgType):
  """Interpret an argument value as a list.

  Intended to be used as the type= for a flag argument. Splits the string on
  commas or another delimiter and returns a list.

  By default, splits on commas:
      'a,b,c' -> ['a', 'b', 'c']
  There is an available syntax for using an alternate delimiter:
      '^:^a,b:c' -> ['a,b', 'c']
      '^::^a:b::c' -> ['a:b', 'c']
      '^,^^a^,b,c' -> ['^a^', ',b', 'c']
  """

  DEFAULT_DELIM_CHAR = ','
  ALT_DELIM_CHAR = '^'

  def __init__(self, element_type=None, min_length=0, max_length=None,
               choices=None):
    """Initialize an ArgList.

    Args:
      element_type: (str)->str, A function to apply to each of the list items.
      min_length: int, The minimum size of the list.
      max_length: int, The maximum size of the list.
      choices: [element_type], a list of valid possibilities for elements. If
          None, then no constraints are imposed.

    Returns:
      (str)->[str], A function to parse the list of values in the argument.

    Raises:
      ArgumentTypeError: If the list is malformed.
    """
    self.element_type = element_type
    self.choices = choices

    if choices:
      def ChoiceType(raw_value):
        if element_type:
          typed_value = element_type(raw_value)
        else:
          typed_value = raw_value
        if typed_value not in choices:
          raise ArgumentTypeError('{value} must be one of [{choices}]'.format(
              value=typed_value, choices=', '.join(
                  [str(choice) for choice in choices])))
        return typed_value
      self.element_type = ChoiceType

    self.min_length = min_length
    self.max_length = max_length

  def __call__(self, arg_value):  # pylint:disable=missing-docstring

    delim = self.DEFAULT_DELIM_CHAR
    if (arg_value.startswith(self.ALT_DELIM_CHAR) and
        self.ALT_DELIM_CHAR in arg_value[1:]):
      delim, arg_value = arg_value[1:].split(self.ALT_DELIM_CHAR, 1)
      if not delim:
        raise ArgumentTypeError(
            'Invalid delimiter. Please see `gcloud topic escaping` for '
            'information on escaping list or dictionary flag values.')
    arg_list = _TokenizeQuotedList(arg_value, delim=delim)

    # TODO(b/35944028): These exceptions won't present well to the user.
    if len(arg_list) < self.min_length:
      raise ArgumentTypeError('not enough args')
    if self.max_length is not None and len(arg_list) > self.max_length:
      raise ArgumentTypeError('too many args')

    if self.element_type:
      arg_list = [self.element_type(arg) for arg in arg_list]

    return arg_list

  def GetUsageMsg(self, is_custom_metavar, metavar):
    is_custom_metavar = is_custom_metavar
    msg = '[{metavar},...]'.format(metavar=metavar)
    if self.min_length:
      msg = ','.join([metavar]*self.min_length+[msg])
    return msg


class ArgDict(ArgList):
  """Interpret an argument value as a dict.

  Intended to be used as the type= for a flag argument. Splits the string on
  commas to get a list, and then splits the items on equals to get a set of
  key-value pairs to get a dict.
  """

  def __init__(self, key_type=None, value_type=None, spec=None, min_length=0,
               max_length=None, allow_key_only=False, required_keys=None,
               operators=None):
    """Initialize an ArgDict.

    Args:
      key_type: (str)->str, A function to apply to each of the dict keys.
      value_type: (str)->str, A function to apply to each of the dict values.
      spec: {str: (str)->str}, A mapping of expected keys to functions.
        The functions are applied to the values. If None, an arbitrary
        set of keys will be accepted. If not None, it is an error for the
        user to supply a key that is not in the spec. If the function specified
        is None, then accept a key only without '=value'.
      min_length: int, The minimum number of keys in the dict.
      max_length: int, The maximum number of keys in the dict.
      allow_key_only: bool, Allow empty values.
      required_keys: [str], Required keys in the dict.
      operators: operator_char -> value_type, Define multiple single character
        operators, each with its own value_type converter. Use value_type==None
        for no conversion. The default value is {'=': value_type}

    Returns:
      (str)->{str:str}, A function to parse the dict in the argument.

    Raises:
      ArgumentTypeError: If the list is malformed.
      ValueError: If both value_type and spec are provided.
    """
    super(ArgDict, self).__init__(min_length=min_length, max_length=max_length)
    if spec and value_type:
      raise ValueError('cannot have both spec and sub_type')
    self.key_type = key_type
    self.spec = spec
    self.allow_key_only = allow_key_only
    self.required_keys = required_keys or []
    if not operators:
      operators = {'=': value_type}
    for op in operators.keys():
      if len(op) != 1:
        raise ArgumentTypeError(
            'Operator [{}] must be one character.'.format(op))
    ops = ''.join(operators.keys())
    key_op_value_pattern = '([^{ops}]+)([{ops}]?)(.*)'.format(
        ops=re.escape(ops))
    self.key_op_value = re.compile(key_op_value_pattern, re.DOTALL)
    self.operators = operators

  def _ApplySpec(self, key, value):
    if key in self.spec:
      if self.spec[key] is None:
        if value:
          raise ArgumentTypeError('Key [{0}] does not take a value'.format(key))
        return None
      return self.spec[key](value)
    else:
      raise ArgumentTypeError(
          _GenerateErrorMessage(
              'valid keys are [{0}]'.format(
                  ', '.join(sorted(self.spec.keys()))),
              user_input=key))

  def __call__(self, arg_value):  # pylint:disable=missing-docstring
    arg_list = super(ArgDict, self).__call__(arg_value)

    arg_dict = {}
    for arg in arg_list:
      match = self.key_op_value.match(arg)
      # TODO(b/35944028): These exceptions won't present well to the user.
      if not match:
        raise ArgumentTypeError('Invalid flag value [{0}]'.format(arg))
      key, op, value = match.group(1), match.group(2), match.group(3)
      if not op and not self.allow_key_only:
        raise ArgumentTypeError(
            ('Bad syntax for dict arg: [{0}]. Please see `gcloud topic '
             'escaping` if you would like information on escaping list or '
             'dictionary flag values.').format(arg))
      if self.key_type:
        try:
          key = self.key_type(key)
        except ValueError:
          raise ArgumentTypeError('Invalid key [{0}]'.format(key))
      convert_value = self.operators.get(op, None)
      if convert_value:
        try:
          value = convert_value(value)
        except ValueError:
          raise ArgumentTypeError('Invalid value [{0}]'.format(value))
      if self.spec:
        value = self._ApplySpec(key, value)
      arg_dict[key] = value

    for required_key in self.required_keys:
      if required_key not in arg_dict:
        raise ArgumentTypeError(
            'Key [{0}] required in dict arg but not provided'.format(
                required_key))

    return arg_dict

  def GetUsageMsg(self, is_custom_metavar, metavar):
    # If we're not using a spec to limit the key values or if metavar
    # has been overridden, then use the normal ArgList formatting
    if not self.spec or is_custom_metavar:
      return super(ArgDict, self).GetUsageMsg(is_custom_metavar, metavar)

    msg_list = []
    spec_list = sorted(self.spec.iteritems())

    # First put the spec keys with no value followed by those that expect a
    # value
    for spec_key, spec_function in spec_list:
      if spec_function is None:
        if not self.allow_key_only:
          raise ArgumentTypeError(
              'Key [{0}] specified in spec without a function but '
              'allow_key_only is set to False'.format(spec_key))
        msg_list.append(spec_key)

    for spec_key, spec_function in spec_list:
      if spec_function is not None:
        msg_list.append('{0}={1}'.format(spec_key, spec_key.upper()))

    msg = '[' + '],['.join(msg_list) + ']'
    return msg


class UpdateAction(argparse.Action):
  r"""Create a single dict value from delimited or repeated flags.

  This class is intended to be a more flexible version of
  argparse._AppendAction.

  For example, with the following flag definition:

      parser.add_argument(
        '--inputs',
        type=arg_parsers.ArgDict(),
        action='append')

  a caller can specify on the command line flags such as:

    --inputs k1=v1,k2=v2

  and the result will be a list of one dict:

    [{ 'k1': 'v1', 'k2': 'v2' }]

  Specifying two separate command line flags such as:

    --inputs k1=v1 \
    --inputs k2=v2

  will produce a list of dicts:

    [{ 'k1': 'v1'}, { 'k2': 'v2' }]

  The UpdateAction class allows for both of the above user inputs to result
  in the same: a single dictionary:

    { 'k1': 'v1', 'k2': 'v2' }

  This gives end-users a lot more flexibility in constructing their command
  lines, especially when scripting calls.

  Note that this class will raise an exception if a key value is specified
  more than once. To allow for a key value to be specified multiple times,
  use UpdateActionWithAppend.
  """

  def OnDuplicateKeyRaiseError(self, key, existing_value=None, new_value=None):
    if existing_value is None:
      user_input = None
    else:
      user_input = ', '.join([existing_value, new_value])
    raise argparse.ArgumentError(self, _GenerateErrorMessage(
        '"{0}" cannot be specified multiple times'.format(key),
        user_input=user_input))

  def __init__(self,
               option_strings,
               dest,
               nargs=None,
               const=None,
               default=None,
               type=None,  # pylint:disable=redefined-builtin
               choices=None,
               required=False,
               help=None,  # pylint:disable=redefined-builtin
               metavar=None,
               onduplicatekey_handler=OnDuplicateKeyRaiseError):
    if nargs == 0:
      raise ValueError('nargs for append actions must be > 0; if arg '
                       'strings are not supplying the value to append, '
                       'the append const action may be more appropriate')
    if const is not None and nargs != argparse.OPTIONAL:
      raise ValueError('nargs must be %r to supply const' % argparse.OPTIONAL)
    self.choices = choices
    if isinstance(choices, dict):
      choices = sorted(choices.keys())
    super(UpdateAction, self).__init__(
        option_strings=option_strings,
        dest=dest,
        nargs=nargs,
        const=const,
        default=default,
        type=type,
        choices=choices,
        required=required,
        help=help,
        metavar=metavar)
    self.onduplicatekey_handler = onduplicatekey_handler

  # pylint: disable=protected-access
  def __call__(self, parser, namespace, values, option_string=None):

    if isinstance(values, dict):
      # Get the existing arg value (if any)
      items = copy.copy(argparse._ensure_value(namespace, self.dest, {}))
      # Merge the new key/value pair(s) in
      for k, v in values.iteritems():
        if k in items:
          v = self.onduplicatekey_handler(self, k, items[k], v)
        items[k] = v
    else:
      # Get the existing arg value (if any)
      items = copy.copy(argparse._ensure_value(namespace, self.dest, []))
      # Merge the new key/value pair(s) in
      for k in values:
        if k in items:
          self.onduplicatekey_handler(self, k)
        else:
          items.append(k)

    # Saved the merged dictionary
    setattr(namespace, self.dest, items)


class UpdateActionWithAppend(UpdateAction):
  """Create a single dict value from delimited or repeated flags.

  This class provides a variant of UpdateAction, which allows for users to
  append, rather than reject, duplicate key values. For example, the user
  can specify:

    --inputs k1=v1a --inputs k1=v1b --inputs k2=v2

  and the result will be:

     { 'k1': ['v1a', 'v1b'], 'k2': 'v2' }
  """

  def OnDuplicateKeyAppend(self, key, existing_value=None, new_value=None):
    if existing_value is None:
      return key
    elif isinstance(existing_value, list):
      return existing_value + [new_value]
    else:
      return [existing_value, new_value]

  def __init__(self,
               option_strings,
               dest,
               nargs=None,
               const=None,
               default=None,
               type=None,  # pylint:disable=redefined-builtin
               choices=None,
               required=False,
               help=None,  # pylint:disable=redefined-builtin
               metavar=None,
               onduplicatekey_handler=OnDuplicateKeyAppend):
    super(UpdateActionWithAppend, self).__init__(
        option_strings=option_strings,
        dest=dest,
        nargs=nargs,
        const=const,
        default=default,
        type=type,
        choices=choices,
        required=required,
        help=help,
        metavar=metavar,
        onduplicatekey_handler=onduplicatekey_handler)


class RemainderAction(argparse._StoreAction):  # pylint: disable=protected-access
  """An action with a couple of helpers to better handle --.

  argparse on its own does not properly handle -- implementation args.
  argparse.REMAINDER greedily steals valid flags before a --, and nargs='*' will
  bind to [] and not  parse args after --. This Action represents arguments to
  be passed through to a subcommand after --.

  Primarily, this Action provides two utility parsers to help a modified
  ArgumentParser parse -- properly.

  There is one additional property:
    example: A usage statement used to construct nice additional help.
  """

  def __init__(self, example=None, *args, **kwargs):
    if kwargs['nargs'] is not argparse.REMAINDER:
      raise ValueError(
          'The RemainderAction should only be used when '
          'nargs=argparse.REMAINDER.')

    # Create detailed help.
    self.explanation = (
        "The '--' argument must be specified between gcloud specific args on "
        'the left and {metavar} on the right.'
    ).format(metavar=kwargs['metavar'])
    if 'help' in kwargs:
      kwargs['help'] += '\n\n' + self.explanation
      if example:
        kwargs['help'] += ' Example:\n\n' + example
    super(RemainderAction, self).__init__(*args, **kwargs)

  def _SplitOnDash(self, args):
    split_index = args.index('--')
    # Remove -- before passing through
    return args[:split_index], args[split_index + 1:]

  def ParseKnownArgs(self, args, namespace):
    """Binds all args after -- to the namespace."""
    # Not [], so that we can distinguish between empty remainder args and
    # absent remainder args.
    remainder_args = None
    if '--' in args:
      args, remainder_args = self._SplitOnDash(args)
    self(None, namespace, remainder_args)
    return namespace, args

  def ParseRemainingArgs(self, remaining_args, namespace, original_args):
    """Parses the unrecognized args from the end of the remaining_args.

    This method identifies all unrecognized arguments after the last argument
    recognized by a parser (but before --). It then either logs a warning and
    binds them to the namespace or raises an error, depending on strictness.

    Args:
      remaining_args: A list of arguments that the parsers did not recognize.
      namespace: The Namespace to bind to.
      original_args: The full list of arguments given to the top parser,

    Raises:
      ArgumentError: If there were remaining arguments after the last recognized
      argument and this action is strict.

    Returns:
      A tuple of the updated namespace and unrecognized arguments (before the
      last recognized argument).
    """
    # Only parse consecutive unknown args from the end of the original args.
    # Strip out everything after '--'
    if '--' in original_args:
      original_args, _ = self._SplitOnDash(original_args)
    # Find common suffix between remaining_args and orginal_args
    split_index = 0
    for i, (arg1, arg2) in enumerate(
        zip(reversed(remaining_args), reversed(original_args))):
      if arg1 != arg2:
        split_index = len(remaining_args) - i
        break
    pass_through_args = remaining_args[split_index:]
    remaining_args = remaining_args[:split_index]

    if pass_through_args:
      msg = (u'unrecognized args: {args}\n' + self.explanation).format(
          args=u' '.join(pass_through_args))
      raise parser_errors.UnrecognizedArgumentsError(msg)
    self(None, namespace, pass_through_args)
    return namespace, remaining_args


class StoreOnceAction(argparse.Action):
  r"""Create a single dict value from delimited flags.

  For example, with the following flag definition:

      parser.add_argument(
        '--inputs',
        type=arg_parsers.ArgDict(),
        action=StoreOnceAction)

  a caller can specify on the command line flags such as:

    --inputs k1=v1,k2=v2

  and the result will be a list of one dict:

    [{ 'k1': 'v1', 'k2': 'v2' }]

  Specifying two separate command line flags such as:

    --inputs k1=v1 \
    --inputs k2=v2

  will raise an exception.

  Note that this class will raise an exception if a key value is specified
  more than once. To allow for a key value to be specified multiple times,
  use UpdateActionWithAppend.
  """

  def OnSecondArgumentRaiseError(self):
    raise argparse.ArgumentError(self, _GenerateErrorMessage(
        '"{0}" argument cannot be specified multiple times'.format(self.dest)))

  def __init__(self, *args, **kwargs):
    self.dest_is_populated = False
    super(StoreOnceAction, self).__init__(*args, **kwargs)

  # pylint: disable=protected-access
  def __call__(self, parser, namespace, values, option_string=None):
    # Make sure no existing arg value exist
    if self.dest_is_populated:
      self.OnSecondArgumentRaiseError()
    self.dest_is_populated = True
    setattr(namespace, self.dest, values)


class _HandleNoArgAction(argparse.Action):
  """This class should not be used directly, use HandleNoArgAction instead."""

  def __init__(self, none_arg, deprecation_message, **kwargs):
    super(_HandleNoArgAction, self).__init__(**kwargs)
    self.none_arg = none_arg
    self.deprecation_message = deprecation_message

  def __call__(self, parser, namespace, value, option_string=None):
    if value is None:
      log.warn(self.deprecation_message)
      if self.none_arg:
        setattr(namespace, self.none_arg, True)

    setattr(namespace, self.dest, value)


def HandleNoArgAction(none_arg, deprecation_message):
  """Creates an argparse.Action that warns when called with no arguments.

  This function creates an argparse action which can be used to gracefully
  deprecate a flag using nargs=?. When a flag is created with this action, it
  simply log.warn()s the given deprecation_message and then sets the value of
  the none_arg to True.

  This means if you use the none_arg no_foo and attach this action to foo,
  `--foo` (no argument), it will have the same effect as `--no-foo`.

  Args:
    none_arg: a boolean argument to write to. For --no-foo use "no_foo"
    deprecation_message: msg to tell user to stop using with no arguments.

  Returns:
    An argparse action.

  """
  def HandleNoArgActionInit(**kwargs):
    return _HandleNoArgAction(none_arg, deprecation_message, **kwargs)

  return HandleNoArgActionInit
