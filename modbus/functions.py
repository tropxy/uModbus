import struct

from modbus import log
from modbus.utils import memoize
from modbus.exceptions import IllegalDataValueError, IllegalDataAddressError

try:
    from functools import reduce
except ImportError:
    pass

# Function related to data access.
READ_COILS = 1
READ_DISCRETE_INPUTS = 2
READ_HOLDING_REGISTERS = 3
READ_INPUT_REGISTERS = 4

WRITE_SINGLE_COIL = 5
WRITE_SINGLE_REGISTER = 6
WRITE_MULTIPLE_COILS = 15
WRITE_SINGLE_REGISTERS = 16

READ_FILE_RECORD = 20
WRITE_FILE_RECORD = 21

MASK_WRITE_REGISTER = 22
READ_WRITE_MULTIPLE_REGISTERS = 23
READ_FIFO_QUEUE = 24

# Diagnostic functions, only available when using serial line.
READ_EXCEPTION_STATUS = 7
DIAGNOSTICS = 8
GET_COMM_EVENT_COUNTER = 11
GET_COM_EVENT_LOG = 12
REPORT_SERVER_ID = 17


@memoize
def function_factory(pdu):
    """ Return function instance, based on request PDU.

    :param pdu: Array of bytes.
    :return: Instance of a function.
    """
    function_code, = struct.unpack('>B', pdu[:1])
    function_class = function_code_to_function_map[function_code]

    return function_class.create_from_request_pdu(pdu)


class ReadFunction(object):
    """ Abstract base class for Modbus read functions. """
    def __init__(self, starting_address, quantity):
        if quantity < 1 or quantity > self.max_quantity:
            raise IllegalDataValueError('Quantify field of request must be a '
                                        'value between 0 and '
                                        '{0}.'.format(self.max_quantity))

        self.starting_address = starting_address
        self.quantity = quantity

    @classmethod
    def create_from_request_pdu(cls, pdu):
        """ Create instance from request PDU.

        :param pdu: A response PDU.
        """
        _, starting_address, quantity = struct.unpack('>BHH', pdu)

        return cls(starting_address, quantity)

    def execute(self, slave_id, route_map):
        """ Execute the Modbus function registered for a route.

        :param slave_id: Slave id.
        :param eindpoint: Instance of modbus.route.Map.
        :return: Result of call to endpoint.
        """
        try:
            values = []

            for address in range(self.starting_address,
                                 self.starting_address + self.quantity):
                endpoint = route_map.match(slave_id, self.function_code,
                                           address)
                values.append(endpoint(slave_id=slave_id, address=address))

            return values
        # route_map.match() returns None if no match is found. Calling None
        # results in TypeError.
        except TypeError:
            raise IllegalDataAddressError()


class WriteSingleValueFunction(object):
    """ Abstract base class for Modbus write functions. """
    def __init__(self, address, value):
        self.address = address
        self.value = value

    @classmethod
    def create_from_request_pdu(cls, pdu):
        """ Create instance from request PDU.

        :param pdu: A response PDU.
        """
        _, address, value = struct.unpack('>BHH', pdu)

        return cls(address, value)

    def execute(self, slave_id, route_map):
        """ Execute the Modbus function registered for a route.

        :param slave_id: Slave id.
        :param eindpoint: Instance of modbus.route.Map.
        """
        endpoint = route_map.match(slave_id, self.function_code, self.address)
        try:
            endpoint(slave_id=slave_id, address=self.address, value=self.value)
        # route_map.match() returns None if no match is found. Calling None
        # results in TypeError.
        except TypeError:
            raise IllegalDataAddressError()

    def create_response_pdu(self):
        return struct.pack('>BHH', self.function_code, self.address,
                           self.value)


class SingleBitResponse(object):
    """ Base class with common logic for so called 'single bit' functions.
    These functions operate on single bit values, like coils and discrete
    inputs.

    """
    def create_response_pdu(self, data):
        """ Create response pdu.

        :param data: A list with 0's and/or 1's.
        :return: Byte array of at least 3 bytes.
        """
        log.debug('Create single bit response pdu {0}.'.format(data))
        bytes_ = [data[i:i + 8] for i in range(0, len(data), 8)]

        # Reduce each all bits per byte to a number. Byte
        # [0, 0, 0, 0, 0, 1, 1, 1] is intepreted as binary en is decimal 3.
        for index, byte in enumerate(bytes_):
            bytes_[index] = \
                reduce(lambda a, b: (a << 1) + b, list(reversed(byte)))

        log.debug('Reduced single bit data to {0}.'.format(bytes_))
        # The first 2 B's of the format encode the function code (1 byte)
        # and the length (1 byte) of the following byte series. Followed by
        # a B for every byte in the series of bytes. 3 lead to the format
        # '>BBB' and 257 lead to the format '>BBBB'.
        fmt = '>BB' + 'B' * len(bytes_)
        return struct.pack(fmt, self.function_code, len(bytes_), *bytes_)


class MultiBitResponse(object):
    """ Base class with common logic for so called 'multi bit' functions.
    These functions operate on byte values, like input registers and holding
    registers. By default values are 16 bit and unsigned.

    """
    def create_response_pdu(self, data):
        """ Create response pdu.

        :param data: A list with values.
        :return: Byte array of at least 4 bytes.
        """
        log.debug('Create multi bit response pdu {0}.'.format(data))
        fmt = '>BB' + 'H' * len(data)

        return struct.pack(fmt, self.function_code, len(data) * 2, *data)


class ReadCoils(ReadFunction, SingleBitResponse):
    """ Implement Modbus function code 01.

    "This function code is used to read from 1 to 2000 contiguous status of
    coils in a remote device. The Request PDU specifies the starting address,
    i.e. the address of the first coil specified, and the number of coils. In
    the PDU Coils are addressed starting at zero. Therefore coils numbered 1-16
    are addressed as 0-15.

    The coils in the response message are packed as one coil per bit of the
    data field. Status is indicated as 1= ON and 0= OFF. The LSB of the first
    data byte contains the output addressed in the query. The other coils
    follow toward the high order end of this byte, and from low order to high
    order in subsequent bytes.

    If the returned output quantity is not a multiple of eight, the remaining
    bits in the final data byte will be padded with zeros (toward the high
    order end of the byte). The Byte Count field specifies the quantity of
    complete bytes of data."

            - MODBUS Application Protocol Specification V1.1b3, chapter 6.1

    The request PDU with function code 01 must be 5 bytes:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Starting address | 2              |
        | Quantity         | 2              |
        +------------------+----------------+

    The PDU can unpacked to this::

        >>> struct.unpack('>BHH', b'\x01\x00d\x00\x03')
        (1, 100, 3)

    The reponse PDU varies in length, depending on the request. Each 8 coils
    require 1 byte. The amount of bytes needed represent status of the coils to
    can be calculated with: bytes = round(quantity / 8) + 1. This response
    contains (3 / 8 + 1) = 1 byte to describe the status of the coils. The
    structure of a compleet response PDU looks like this:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Byte count       | 1              |
        | Coil status      | n              |
        +------------------+----------------+

    Assume the status of 102 is 0, 101 is 1 and 100 is also 1. This is binary
    011 which is decimal 3.

    The PDU can packed like this::

        >>> struct.pack('>BBB', function_code, byte_count, 3)
        b'\x01\x01\x03'

    """
    function_code = READ_COILS
    max_quantity = 2000

    def __init__(self, starting_address, quantity):
        ReadFunction.__init__(self, starting_address, quantity)


class ReadDiscreteInputs(ReadFunction, SingleBitResponse):
    """ Implement Modbus function code 02.

    "This function code is used to read from 1 to 2000 contiguous status of
    discrete inputs in a remote device. The Request PDU specifies the starting
    address, i.e. the address of the first input specified, and the number of
    inputs. In the PDU Discrete Inputs are addressed starting at zero.
    Therefore Discrete inputs numbered 1-16 are addressed as 0-15.

    The discrete inputs in the response message are packed as one input per bit
    of the data field.  Status is indicated as 1= ON; 0= OFF. The LSB of the
    first data byte contains the input addressed in the query. The other inputs
    follow toward the high order end of this byte, and from low order to high
    order in subsequent bytes.

    If the returned input quantity is not a multiple of eight, the remaining
    bits in the final d ata byte will be padded with zeros (toward the high
    order end of the byte). The Byte Count field specifies the quantity of
    complete bytes of data."

            - MODBUS Application Protocol Specification V1.1b3, chapter 6.2

    The request PDU with function code 02 must be 5 bytes:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Starting address | 2              |
        | Quantity         | 2              |
        +------------------+----------------+

    The PDU can unpacked to this::

        >>> struct.unpack('>BHH', b'\x02\x00d\x00\x03')
        (2, 100, 3)

    The reponse PDU varies in length, depending on the request. 8 inputs
    require 1 byte. The amount of bytes needed represent status of the inputs
    to can be calculated with: bytes = round(quantity / 8) + 1. This response
    contains (3 / 8 + 1) = 1 byte to describe the status of the inputs. The
    structure of a compleet response PDU looks like this:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Byte count       | 1              |
        | Input status     | n              |
        +------------------+----------------+

    Assume the status of 102 is 0, 101 is 1 and 100 is also 1. This is binary
    011 which is decimal 3.

    The PDU can packed like this::

        >>> struct.pack('>BBB', function_code, byte_count, 3)
        b'\x02\x01\x03'

    """
    function_code = READ_DISCRETE_INPUTS
    max_quantity = 2000

    def __init__(self, starting_address, quantity):
        ReadFunction.__init__(self, starting_address, quantity)


class ReadHoldingRegisters(ReadFunction, MultiBitResponse):
    """ Implement Modbus function code 03.

    "This function code is used to read the contents of a contiguous block of
    holding registers in a remote device. The Request PDU specifies the
    starting register address and the number of registers. In the PDU Registers
    are addressed starting at zero. Therefore registers numbered 1-16 are
    addressed as 0-15.

    The register data in the response message are packed as two bytes per
    register, with the binary contents right justified within each byte. For
    each register, the first byte contains the high order bits and the second
    contains the low order bits."

            - MODBUS Application Protocol Specification V1.1b3, chapter 6.3

    The request PDU with function code 03 must be 5 bytes:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Starting address | 2              |
        | Quantity         | 2              |
        +------------------+----------------+

    The PDU can unpacked to this::

        >>> struct.unpack('>BHH', b'\x03\x00d\x00\x03')
        (3, 100, 3)

    The reponse PDU varies in length, depending on the request. By default,
    holding registers are 16 bit (2 bytes) values. So values of 3 holding
    registers is expressed in 2 * 3 = 6 bytes.

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Byte count       | 1              |
        | Register value   | quantity * 2   |
        +------------------+----------------+

    Assume the value of 100 is 8, 101 is 0 and 102 is also 15.

    The PDU can packed like this::

        >>> data = [8, 0, 15]
        >>> struct.pack('>BBHHH', function_code, len(data) * 2, *data)
        '\x03\x06\x00\x08\x00\x00\x00\x0f'

    """
    function_code = READ_HOLDING_REGISTERS
    max_quantity = 125

    def __init__(self, starting_address, quantity):
        ReadFunction.__init__(self, starting_address, quantity)


class ReadInputRegisters(ReadFunction, MultiBitResponse):
    """ Implement Modbus function code 04.

    "This function code is used to read from 1 to 125 contiguous input
    registers in a remote device. The Request PDU specifies the starting
    register address and the number of registers. In the PDU Registers are
    addressed starting at zero. Therefore input registers numbered 1-16 are
    addressed as 0-15.

    The register data in the response message are packed as two bytes
    per register, with the binary contents right justified within each byte.
    For each register, the first byte contains the high order bits and the
    second contains the low order bits."

            - MODBUS Application Protocol Specification V1.1b3, chapter 6.4

    The request PDU with function code 04 must be 5 bytes:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Starting address | 2              |
        | Quantity         | 2              |
        +------------------+----------------+

    The PDU can unpacked to this::

        >>> struct.unpack('>BHH', b'\x04\x00d\x00\x03')
        (4, 100, 3)

    The reponse PDU varies in length, depending on the request. By default,
    holding registers are 16 bit (2 bytes) values. So values of 3 holding
    registers is expressed in 2 * 3 = 6 bytes.

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Byte count       | 1              |
        | Register value   | quantity * 2   |
        +------------------+----------------+

    Assume the value of 100 is 8, 101 is 0 and 102 is also 15.

    The PDU can packed like this::

        >>> data = [8, 0, 15]
        >>> struct.pack('>BBHHH', function_code, len(data) * 2, *data)
        '\x04\x06\x00\x08\x00\x00\x00\x0f'

    """
    function_code = READ_INPUT_REGISTERS
    max_quantity = 125

    def __init__(self, starting_address, quantity):
        ReadFunction.__init__(self, starting_address, quantity)


class WriteSingleCoil(WriteSingleValueFunction):
    """ Implement Modbus function code 05.

    "This function code is used to write a single output to either ON or OFF in
    a remote device. The requested ON/OFF state is specified by a constant in
    the request data field. A value of FF 00 hex requests the output to be ON.
    A value of 00 00 requests it to be OFF. All other values are illegal and
    will not affect the output.

    The Request PDU specifies the address of the coil to be forced. Coils are
    addressed starting at zero. Therefore coil numbered 1 is addressed as 0.
    The requested ON/OFF state is specified by a constant in the Coil Value
    field. A value of 0XFF00 requests the coil to be ON. A value of 0X0000
    requests the coil to be off. All other values are illegal and will not
    affect the coil.

    The normal response is an echo of the request, returned after the coil
    state has been written "

            - MODBUS Application Protocol Specification V1.1b3, chapter 6.5

    The request PDU with function code 05 must be 5 bytes:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Address          | 2              |
        | Value            | 2              |
        +------------------+----------------+

    The PDU can unpacked to this::

        >>> struct.unpack('>BHH', b'\x05\x00d\xFF\x00')
        (5, 100, 65280)

    The reponse PDU is a copy of the request PDU.

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Address          | 2              |
        | Value            | 2              |
        +------------------+----------------+

    """
    function_code = WRITE_SINGLE_COIL

    def __init__(self, address, value):
        WriteSingleValueFunction.__init__(self, address, value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        """ Validate if value is 0 or 0xFF00. """
        if value not in [0, 0xFF00]:
            raise IllegalDataValueError

        self._value = value


class WriteSingleRegister(WriteSingleValueFunction):
    """ Implement Modbus function code 06.

    "This function code is used to write a single holding register in a remote
    device. The Request PDU specifies the address of the register to be
    written. Registers are addressed starting at zero. Therefore register
    numbered 1 is addressed as 0. The normal response is an echo of the
    request, returned after the register contents have been written."

            - MODBUS Application Protocol Specification V1.1b3, chapter 6.6

    The request PDU with function code 06 must be 5 bytes:

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Address          | 2              |
        | Value            | 2              |
        +------------------+----------------+

    The PDU can unpacked to this::

        >>> struct.unpack('>BHH', b'\x05\x00d\x00\x03')
        (6, 100, 3)

    The reponse PDU is a copy of the request PDU.

        +------------------+----------------+
        | Field            | Length (bytes) |
        +------------------+----------------+
        | Function code    | 1              |
        | Address          | 2              |
        | Value            | 2              |
        +------------------+----------------+

    """
    function_code = WRITE_SINGLE_REGISTER

    def __init__(self, address, value):
        WriteSingleValueFunction.__init__(self, address, value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        """ Validate if value is in range of 0 between 0xFFFF (which is maximum
        a number a 16 bit number can be).
        """
        if 0 <= value <= 0xFFFF:
            self._value = value
        else:
            raise IllegalDataValueError

function_code_to_function_map = {
    READ_COILS: ReadCoils,
    READ_DISCRETE_INPUTS: ReadDiscreteInputs,
    READ_HOLDING_REGISTERS: ReadHoldingRegisters,
    READ_INPUT_REGISTERS: ReadInputRegisters,
    WRITE_SINGLE_COIL: WriteSingleCoil,
    WRITE_SINGLE_REGISTER: WriteSingleRegister,
}
