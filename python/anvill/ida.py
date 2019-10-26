# Copyright (c) 2019 Trail of Bits, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import weakref


import ida_bytes
import ida_funcs
import ida_ida
import ida_idaapi
import ida_nalt
import ida_idp
import ida_typeinf


from .arch import *
from .exc import *
from .function import *
from .os import *
from .type import *


def _guess_os():
  """Try to guess the current OS"""
  abi_name = ida_nalt.get_abi_name()
  if "OSX" == abi_name:
    return "macos"

  inf = ida_idaapi.get_inf_structure()
  file_type = inf.filetype
  if file_type in (ida_ida.f_ELF, ida_ida.g_AOUT, ida_ida.f_COFF):
    return "linux"
  elif file_type == ida_ida.g_MACHO:
    return "macos"
  elif file_type in (ida_ida.g_PE, ida_ida.f_EXE, ida_ida.f_EXE_old, ida_ida.f_COM, ida_ida.f_COM_old):
    return "windows"
  else:
    raise UnhandledOSException("Unrecognized OS type")


def _guess_architecture():
  """Try to guess the current architecture."""

  reg_names = ida_idp.ph_get_regnames()
  inf = ida_idaapi.get_inf_structure()

  if "ax" in reg_names and "xmm0" in reg_names:
    if inf.is_64bit():
      return "amd64"
    else:
      return "x86"

  elif "ARM" in info.procName:
    if inf.is_64bit():
      return "aarch64"
    else:
      raise UnhandledArchitectureType(
          "Unrecognized 32-bit ARM architecture: {}".format(inf.procName))
  else:
    raise UnhandledArchitectureType(
        "Unrecognized archictecture: {}".format(inf.procName))


def _convert_ida_type(ty, cache):
  """Convert an IDA `tinfo_t` instance into a `Type` instance."""
  assert isinstance(ty, ida_typeinf.tinfo_t)

  if ty in cache:
    return cache[ty]

  # Void type.
  if ty.empty() or ty.is_void():
    return VoidType()

  # Pointer, array, or function.
  elif ty.is_paf():
    if ty.is_ptr():
      ret = PointerType()
      cache[ty] = ret
      ret.set_element_type(_convert_ida_type(ty.get_pointed_object(), cache))
      return ret

    elif ty.is_func():
      ret = FunctionType()
      cache[ty] = ret
      ret.set_return_type(_convert_ida_type(ty.get_rettype(), cache))
      i = 0
      max_i = ty.get_nargs()
      while i < max_i:
        ret.add_parameter_type(_convert_ida_type(ty.get_nth_arg(i), cache))
        i += 1

      if ty.is_vararg_cc():
        ret.set_is_vararg()

      if ty.is_purging_cc():
        ret.set_num_bytes_popped_off_stack(ty.calc_purged_bytes())

      return ret

    elif ty.is_array():
      ret = ArrayType()
      cache[ty] = ret
      ret.set_element_type(_convert_ida_type(ty.get_array_element(), cache))
      ret.set_num_elements(ty.get_array_nelems())
      return ret

    else:
      raise UnhandledTypeException(
          "Unhandled pointer, array, or function type: {}".format(ty.dstr()), ty)

  # Vector types.
  elif ty.is_sse_type():
    ret = VectorType()
    cache[ty] = ret
    size = ty.get_size()

    # TODO(pag): Do better than this.
    ret.set_element_type(IntegerType(1, False))
    ret.set_num_elements(size)

    return ret

  # Structure, union, or enumerator.
  elif ty.is_sue():
    if ty.is_udt():  # Structure or union type.
      ret = ty.is_struct() and StructureType() or UnionType()
      cache[ty] = ret
      i = 0
      max_i = ty.get_udt_nmembers()
      while i < max_i:
        udt = ida_typeinf.udt_member_t()
        udt.offset = i
        if not ty.find_udt_member(udt, ida_typeinf.STRMEM_INDEX):
          break
        # TODO(pag): bitfields
        # TODO(pag): padding
        ret.add_element_type(_convert_ida_type(udt.type, cache))
        i += 1
      return ret

    elif ty.is_enum():
      ret = EnumType()
      cache[ty] = ret
      base_type = ida_typeinf.tinfo_t(ty.get_enum_base_type())
      ret.set_underlying_type(_convert_ida_type(base_type, cache))
      return ret

    else:
      raise UnhandledTypeException(
          "Unhandled struct, union, or enum type: {}".format(ty.dstr()), ty)
  
  # Boolean type.
  elif ty.is_bool():
    return BoolType()
  
  # Integer type.
  elif ty.is_integral():
    if ty.is_uint128():
      return IntegerType(16, False)
    elif ty.is_int128():
      return IntegerType(16, True)
    elif ty.is_uint64():
      return IntegerType(8, False)
    elif ty.is_int64():
      return IntegerType(8, True)
    elif ty.is_uint32():
      return IntegerType(4, False)
    elif ty.is_int32():
      return IntegerType(4, True)
    elif ty.is_uint16():
      return IntegerType(2, False)
    elif ty.is_int16():
      return IntegerType(2, True)
    elif ty.is_uchar():
      return IntegerType(1, False)
    elif ty.is_char():
      return IntegerType(1, True)
    else:
      raise UnhandledTypeException("Unhandled integral type: {}".format(ty.dstr()), ty)

  # Floating point.
  elif ty.is_floating():
    if ty.is_ldouble():
      return FloatingPointType(ty.get_unpadded_size())
    elif ty.is_double():
      return FloatingPointType(8)
    elif ty.is_float():
      return FloatingPointType(4)
    else:
      raise UnhandledTypeException(
          "Unhandled floating point type: {}".format(ty.dstr()), ty)

  elif ty.is_complex():
    raise UnhandledTypeException(
        "Complex numbers are not yet handled: {}".format(ty.dstr()), ty)

  # Type alias/reference.
  elif ty.is_typeref():
    ret = TypedefType()
    cache[ty] = ret
    ret.set_underlying_type(_convert_ida_type(ty.get_realtype(), cache))
    return ret

  else:
    raise UnhandledTypeException(
        "Unhandled type: {}".format(ty.dstr()), ty)


def get_arch():
  """Arch class that gives access to architecture-specific functionality."""
  name = _guess_architecture()
  if name == "amd64":
    return AMD64Arch()
  elif name == "x86":
    return X86Arch()
  elif name == "aarch64":
    return AArch64Arch()
  else:
    raise UnhandledArchitectureType(
        "Missing architecture object type for architecture '{}'".format(name))


def get_os():
  """OS class that gives access to OS-specific functionality."""
  name = _guess_os()
  if name == "linux":
    return LinuxOS()
  elif name == "macos":
    return MacOS()
  elif name == "windows":
    return WindowsOS()
  else:
    raise UnhandledOSException(
        "Missing operating system object type for OS '{}'".format(name))


def get_type(ty):
  """Type class that gives access to type sizes, printings, etc."""
  
  if isinstance(ty, Type):
    return ty

  elif isinstance(ty, Function):
    return ty.type()

  elif isinstance(ty, ida_typeinf.tinfo_t):
    return _convert_ida_type(ty, {})

  tif = ida_typeinf.tinfo_t()
  try:
    if not ida_nalt.get_tinfo(tif, ty):
      ida_typeinf.guess_tinfo(tif, ty)
  except:
    pass

  if not tif.empty():
    return _convert_ida_type(tif, {})

  if not ty:
    return VoidType()

  raise UnhandledTypeException("Unrecognized type passed to `Type`.", ty)


class IDAFunction(Function):
  def __init__(self, arch, address, func_type, ida_func):
    super(IDAFunction, self).__init__(arch, address, func_type)
    self._ida_func = ida_func

  def name(self):
    ea = self.address()
    if ida_bytes.f_has_name(ea):
      return ida_funcs.get_func_name(ea)
    else:
      return ""


_FUNCTIONS = weakref.WeakValueDictionary()


def get_function(arch, address):
  """Given an architecture and an address, return a `Function` instance or
  raise an `InvalidFunction` exception."""
  global _FUNCTIONS

  ida_func = ida_funcs.get_func(address)
  if not ida_func:
    ida_func = ida_funcs.get_prev_func(address)

  # Check this function.
  if not ida_func or not ida_funcs.func_contains(ida_func, address):
    raise InvalidFunction(
        "No function defined at or containing address {:x}".format(address))

  # Reset to the start of the function, and get the type of the function.
  address = ida_func.start_ea
  
  try:
    func_type = get_type(address)
  except UnhandledTypeException as e:
    raise InvalidFunction(
        "Could not assign type to function at address {:x}: {}".format(
            address, str(e)))

  print(func_type.serialize(arch, {}))

  if address in _FUNCTIONS:
    return _FUNCTIONS[address]
  else:
    func = IDAFunction(arch, address, func_type, ida_func)
    _FUNCTIONS[address] = func
    return func
