"""
BON (Binary Object Notation) 协议实现
版本: v1.0
兼容 Python 3.7+
"""

import struct
import sys
from decimal import Decimal
from typing import Any, Dict, List, Tuple, Union, Optional, BinaryIO, IO
from io import BytesIO

# 类型标记
TYPE_NONE = 0x00
TYPE_BOOL = 0x01
TYPE_NUMBER = 0x02
TYPE_FLOAT = 0x03
TYPE_STRING = 0x04
TYPE_ARRAY = 0x05
TYPE_OBJECT = 0x06
TYPE_BLOB = 0x07
TYPE_DECIMAL = 0x08

# 魔数
MAGIC = b"bon"

# 类型名称映射（用于错误信息）
TYPE_NAMES = {
    TYPE_NONE: "NONE",
    TYPE_BOOL: "BOOL",
    TYPE_NUMBER: "NUMBER",
    TYPE_FLOAT: "FLOAT",
    TYPE_STRING: "STRING",
    TYPE_ARRAY: "ARRAY",
    TYPE_OBJECT: "OBJECT",
    TYPE_BLOB: "BLOB",
    TYPE_DECIMAL: "DECIMAL",
}


class BONEncoder:
    """BON 编码器"""
    
    def __init__(self, check_circular: bool = True):
        self.check_circular = check_circular
        self._seen = set()
    
    def encode(self, obj: Any) -> bytes:
        """将 Python 对象编码为 BON 字节流"""
        if self.check_circular:
            self._seen.clear()
        return self._encode_value(obj)
    
    def _encode_value(self, obj: Any) -> bytes:
        """编码单个值"""
        # None
        if obj is None:
            return bytes([TYPE_NONE])
        
        # bool
        if isinstance(obj, bool):
            return bytes([TYPE_BOOL, 1 if obj else 0])
        
        # int
        if isinstance(obj, int):
            return self._encode_number(obj)
        
        # float
        if isinstance(obj, float):
            return bytes([TYPE_FLOAT]) + struct.pack("<d", obj)
        
        # Decimal
        if isinstance(obj, Decimal):
            return self._encode_decimal(obj)
        
        # str
        if isinstance(obj, str):
            data = obj.encode("utf-8")
            return bytes([TYPE_STRING]) + struct.pack("<Q", len(data)) + data
        
        # bytes / bytearray
        if isinstance(obj, (bytes, bytearray)):
            data = bytes(obj)
            return bytes([TYPE_BLOB]) + struct.pack("<Q", len(data)) + data
        
        # list / tuple
        if isinstance(obj, (list, tuple)):
            return self._encode_array(obj)
        
        # dict
        if isinstance(obj, dict):
            return self._encode_object(obj)
        
        raise TypeError(f"Unsupported type: {type(obj)}")
    
    def _encode_number(self, num: int) -> bytes:
        """编码任意精度整数"""
        if num >= 0:
            # 正数：直接小端编码
            data = self._int_to_bytes(num)
            return bytes([TYPE_NUMBER]) + struct.pack("<Q", len(data)) + data
        else:
            # 负数：补码表示
            # 计算需要的字节数（比正数多1位用于符号）
            abs_num = abs(num)
            byte_len = (abs_num.bit_length() + 8) // 8 + 1
            # 生成补码
            mask = (1 << (byte_len * 8)) - 1
            complement = (num + mask + 1) & mask
            data = complement.to_bytes(byte_len, "little")
            return bytes([TYPE_NUMBER]) + struct.pack("<Q", len(data)) + data
    
    def _encode_decimal(self, dec: Decimal) -> bytes:
        """编码 Decimal"""
        # 分离整数和小数部分
        if dec < 0:
            sign = -1
            dec = abs(dec)
        else:
            sign = 1
        
        # 获取整数部分和小数部分
        int_part = int(dec)
        frac_part = int((dec - int_part) * 10**9)  # 9位小数精度
        
        # 如果小数部分有更多位数，我们需要计算实际的小数位数
        # 使用 Decimal 的 as_tuple 方法获取更精确的小数
        if dec != int(dec):
            # 获取小数位数
            digits = dec.as_tuple().digits
            exponent = dec.as_tuple().exponent
            if exponent < 0:
                # 有小数部分
                frac_str = ''.join(str(d) for d in digits[-(-exponent):])
                frac_part = int(frac_str) if frac_str else 0
            else:
                frac_part = 0
        else:
            frac_part = 0
        
        int_part = int_part * sign
        return bytes([TYPE_DECIMAL]) + struct.pack("<qQ", int_part, frac_part)
    
    def _encode_array(self, arr: Union[list, tuple]) -> bytes:
        """编码数组"""
        elements = []
        total_len = 0
        
        for item in arr:
            encoded = self._encode_value(item)
            elements.append(encoded)
            total_len += len(encoded)
        
        # 构建数组数据
        result = bytes([TYPE_ARRAY])
        result += struct.pack("<Q", total_len)  # total_len
        result += struct.pack("<Q", len(arr))   # elem_count
        result += b"".join(elements)
        return result
    
    def _encode_object(self, obj: dict) -> bytes:
        """编码对象"""
        pairs = []
        total_len = 0
        
        for key, value in obj.items():
            # 键必须是可哈希的，且不能是 OBJECT
            if isinstance(key, dict):
                raise TypeError("OBJECT cannot be used as key")
            
            # 如果是列表，转为元组
            if isinstance(key, list):
                key = tuple(key)
            
            # 编码键和值
            encoded_key = self._encode_value(key)
            encoded_value = self._encode_value(value)
            
            pairs.append(encoded_key)
            pairs.append(encoded_value)
            total_len += len(encoded_key) + len(encoded_value)
        
        result = bytes([TYPE_OBJECT])
        result += struct.pack("<Q", total_len)  # total_len
        result += struct.pack("<Q", len(obj))   # pair_count
        result += b"".join(pairs)
        return result
    
    @staticmethod
    def _int_to_bytes(num: int) -> bytes:
        """将整数转换为小端字节序列"""
        if num == 0:
            return b"\x00"
        
        # 计算需要的字节数
        byte_len = (num.bit_length() + 7) // 8
        return num.to_bytes(byte_len, "little")


class BONDecoder:
    """BON 解码器"""
    
    def __init__(self):
        self._buffer = b""
        self._pos = 0
    
    def decode(self, data: bytes) -> Any:
        """从 BON 字节流解码为 Python 对象"""
        self._buffer = data
        self._pos = 0
        
        # 检查魔数
        if not self._buffer.startswith(MAGIC):
            raise ValueError("Invalid magic number")
        self._pos = len(MAGIC)
        
        # 检查根类型
        if self._pos >= len(self._buffer):
            raise ValueError("Unexpected end of data")
        root_type = self._buffer[self._pos]
        if root_type != TYPE_OBJECT:
            raise ValueError(f"Root must be OBJECT, got {TYPE_NAMES.get(root_type, 'unknown')}")
        
        return self._decode_value()
    
    def _read_uint64(self) -> int:
        """读取 8 字节无符号整数（小端）"""
        if self._pos + 8 > len(self._buffer):
            raise ValueError("Unexpected end of data")
        value = struct.unpack_from("<Q", self._buffer, self._pos)[0]
        self._pos += 8
        return value
    
    def _read_bytes(self, length: int) -> bytes:
        """读取指定长度的字节"""
        if self._pos + length > len(self._buffer):
            raise ValueError("Unexpected end of data")
        data = self._buffer[self._pos:self._pos + length]
        self._pos += length
        return data
    
    def _decode_value(self) -> Any:
        """解码一个值"""
        if self._pos >= len(self._buffer):
            raise ValueError("Unexpected end of data")
        
        type_marker = self._buffer[self._pos]
        self._pos += 1
        
        if type_marker == TYPE_NONE:
            return None
        
        elif type_marker == TYPE_BOOL:
            if self._pos >= len(self._buffer):
                raise ValueError("Unexpected end of data")
            value = self._buffer[self._pos]
            self._pos += 1
            return bool(value)
        
        elif type_marker == TYPE_NUMBER:
            return self._decode_number()
        
        elif type_marker == TYPE_FLOAT:
            if self._pos + 8 > len(self._buffer):
                raise ValueError("Unexpected end of data")
            value = struct.unpack_from("<d", self._buffer, self._pos)[0]
            self._pos += 8
            return value
        
        elif type_marker == TYPE_STRING:
            length = self._read_uint64()
            data = self._read_bytes(length)
            return data.decode("utf-8")
        
        elif type_marker == TYPE_ARRAY:
            return self._decode_array()
        
        elif type_marker == TYPE_OBJECT:
            return self._decode_object()
        
        elif type_marker == TYPE_BLOB:
            length = self._read_uint64()
            return self._read_bytes(length)
        
        elif type_marker == TYPE_DECIMAL:
            if self._pos + 16 > len(self._buffer):
                raise ValueError("Unexpected end of data")
            int_part = struct.unpack_from("<q", self._buffer, self._pos)[0]
            frac_part = struct.unpack_from("<Q", self._buffer, self._pos + 8)[0]
            self._pos += 16
            
            if int_part >= 0:
                return Decimal(f"{int_part}.{frac_part:09d}")
            else:
                return Decimal(f"{int_part}.{frac_part:09d}")
        
        else:
            raise ValueError(f"Unknown type marker: 0x{type_marker:02X}")
    
    def _decode_number(self) -> int:
        """解码任意精度整数"""
        length = self._read_uint64()
        data = self._read_bytes(length)
        
        if length == 0:
            return 0
        
        # 检查最高位（符号位）
        if data[-1] & 0x80:
            # 负数（补码）
            # 扩展符号位
            extended = bytearray(data)
            if len(extended) < 8:
                # 扩展到 8 字节
                extended.extend([0xFF] * (8 - len(extended)))
            else:
                # 扩展到下一个 8 字节边界
                extra_len = (8 - len(extended) % 8) % 8
                if extra_len > 0:
                    extended.extend([0xFF] * extra_len)
            
            # 从补码转换
            value = int.from_bytes(extended, "little")
            mask = (1 << (len(extended) * 8)) - 1
            return value - mask - 1
        else:
            # 正数
            return int.from_bytes(data, "little")
    
    def _decode_array(self) -> list:
        """解码数组"""
        total_len = self._read_uint64()
        elem_count = self._read_uint64()
        
        end_pos = self._pos + total_len
        result = []
        
        for _ in range(elem_count):
            if self._pos > end_pos:
                raise ValueError("Array data overrun")
            value = self._decode_value()
            result.append(value)
        
        if self._pos != end_pos:
            raise ValueError("Array data size mismatch")
        
        return result
    
    def _decode_object(self) -> dict:
        """解码对象"""
        total_len = self._read_uint64()
        pair_count = self._read_uint64()
        
        end_pos = self._pos + total_len
        result = {}
        
        for _ in range(pair_count):
            if self._pos > end_pos:
                raise ValueError("Object data overrun")
            
            # 解码键
            key = self._decode_value()
            
            # 如果键是列表，转为元组
            if isinstance(key, list):
                key = tuple(key)
            elif isinstance(key, dict):
                raise ValueError("OBJECT cannot be used as key")
            
            # 解码值
            value = self._decode_value()
            
            result[key] = value
        
        if self._pos != end_pos:
            raise ValueError("Object data size mismatch")
        
        return result


def dumps(obj: Any, check_circular: bool = True) -> bytes:
    """
    将 Python 对象序列化为 BON 字节串
    
    Args:
        obj: 要序列化的 Python 对象
        check_circular: 是否检查循环引用
    
    Returns:
        BON 格式的字节串
    
    Raises:
        TypeError: 如果对象包含不支持的类型
        ValueError: 如果检测到循环引用
    """
    encoder = BONEncoder(check_circular)
    encoded = encoder.encode(obj)
    
    # 添加魔数和根类型
    result = MAGIC + bytes([TYPE_OBJECT]) + encoded[1:]  # 跳过 OBJECT 标记
    
    return result


def dump(obj: Any, fp: BinaryIO, check_circular: bool = True) -> None:
    """
    将 Python 对象序列化为 BON 并写入文件
    
    Args:
        obj: 要序列化的 Python 对象
        fp: 二进制文件对象
        check_circular: 是否检查循环引用
    """
    data = dumps(obj, check_circular)
    fp.write(data)


def loads(data: bytes) -> Any:
    """
    从 BON 字节串反序列化为 Python 对象
    
    Args:
        data: BON 格式的字节串
    
    Returns:
        Python 对象
    """
    decoder = BONDecoder()
    return decoder.decode(data)


def load(fp: BinaryIO) -> Any:
    """
    从文件读取 BON 数据并反序列化为 Python 对象
    
    Args:
        fp: 二进制文件对象
    
    Returns:
        Python 对象
    """
    data = fp.read()
    return loads(data)


class BON:
    """
    提供类似 json 模块的 API
    """
    
    @staticmethod
    def dumps(obj: Any, check_circular: bool = True) -> bytes:
        return dumps(obj, check_circular)
    
    @staticmethod
    def dump(obj: Any, fp: BinaryIO, check_circular: bool = True) -> None:
        return dump(obj, fp, check_circular)
    
    @staticmethod
    def loads(data: bytes) -> Any:
        return loads(data)
    
    @staticmethod
    def load(fp: BinaryIO) -> Any:
        return load(fp)