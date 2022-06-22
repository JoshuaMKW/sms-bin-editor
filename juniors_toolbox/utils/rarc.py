import os
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from io import BytesIO
from itertools import chain
from pathlib import Path, PurePath
from struct import pack, unpack
from typing import BinaryIO, Optional

from enum import IntFlag
from juniors_toolbox.utils import (A_Serializable, ReadableBuffer,
                                   VariadicArgs, VariadicKwargs, jdrama)
from juniors_toolbox.utils.iohelper import (read_bool, read_sint16,
                                            read_sint32, read_string,
                                            read_uint16, read_uint32,
                                            write_bool, write_sint16,
                                            write_sint32, write_string,
                                            write_uint16, write_uint32)
from numpy import append


def write_pad32(f: BinaryIO):
    next_aligned_pos = (f.tell() + 0x1F) & ~0x1F
    f.write(b"\x00"*(next_aligned_pos - f.tell()))


class ResourceAttribute(IntFlag):
    FILE = 0x01
    DIRECTORY = 0x02
    COMPRESSED = 0x04
    PRELOAD_TO_MRAM = 0x10
    PRELOAD_TO_ARAM = 0x20
    LOAD_FROM_DVD = 0x40
    YAZ0_COMPRESSED = 0x80  # Uses YAZ0 compression


class A_ResourceHandle():

    @dataclass
    class _DataInformation:
        data: BytesIO
        offsets: dict["A_ResourceHandle", int]
        mramSize: int
        aramSize: int
        dvdSize: int

    @dataclass
    class _LoadSortedHandles:
        mram: list["A_ResourceHandle"]
        aram: list["A_ResourceHandle"]
        dvd: list["A_ResourceHandle"]

    def __init__(
        self,
        name: str,
        parent: Optional["ResourceDirectory"] = None,
        attributes: ResourceAttribute = ResourceAttribute.PRELOAD_TO_MRAM
    ):
        self._archive: Optional["ResourceArchive"] = None
        self._name = name

        self._parent = parent
        if parent is not None:
            self._archive = parent._archive

        self._attributes = attributes

    def is_flagged(self, attribute: ResourceAttribute | int) -> bool:
        return (self._attributes & attribute) != 0

    def get_flags(self) -> ResourceAttribute:
        return self._attributes

    def set_flag(self, attribute: ResourceAttribute, active: bool) -> None:
        if active:
            self._attributes |= attribute
        else:
            self._attributes &= attribute

    def get_name(self) -> str:
        return self._name

    def set_name(self, name: str):
        self._name = name

    def get_extension(self) -> str:
        return self._name.split(".")[-1]

    def set_extension(self, extension: str):
        parts = self._name.split(".")
        parts[-1] = extension
        self._name = ".".join(parts)

    def get_stem(self) -> str:
        if "." not in self._name:
            return self._name
        return ".".join(self._name.split(".")[:-1])

    def set_stem(self, stem: str):
        if "." not in self._name:
            self._name = stem
            return

        index = -1
        extIndex = 0
        while (index := self._name.find(".", index+1)) != -1:
            extIndex = index

        self._name = stem + self._name[extIndex:]

    def get_path(self) -> PurePath:
        path = PurePath(self.get_name())
        parent = self.get_parent()
        while parent is not None:
            path = parent.get_name() / path
            parent = parent.get_parent()
        return path

    def get_archive(self) -> Optional["ResourceArchive"]:
        return self._archive

    def get_parent(self) -> Optional["ResourceDirectory"]:
        return self._parent

    def set_parent(self, handle: "ResourceDirectory"):
        handle.add_handle(self)

    @abstractmethod
    def is_directory(self) -> bool: ...

    @abstractmethod
    def is_file(self) -> bool: ...

    @abstractmethod
    def sync_ids(self) -> bool: ...

    @abstractmethod
    def get_magic(self) -> str: ...

    @abstractmethod
    def get_id(self) -> int: ...

    @abstractmethod
    def set_id(self, __id: int, /) -> None: ...

    @abstractmethod
    def get_size(self) -> int: ...

    @abstractmethod
    def get_data(self) -> bytes: ...

    @abstractmethod
    def get_raw_data(self) -> bytes: ...

    @abstractmethod
    def get_handles(
        self, *, flatten: bool = False) -> list["A_ResourceHandle"]: ...

    @abstractmethod
    def get_handle(self, __path: PurePath | str, /
                   ) -> Optional["A_ResourceHandle"]: ...

    @abstractmethod
    def path_exists(self, __path: PurePath | str, /) -> bool: ...

    @abstractmethod
    def add_handle(self, __handle: "A_ResourceHandle", /) -> bool: ...

    @abstractmethod
    def remove_handle(self, __handle: "A_ResourceHandle", /) -> bool: ...

    @abstractmethod
    def remove_path(self, __path: PurePath | str, /) -> bool: ...

    @abstractmethod
    def new_file(
        self,
        name: str,
        initialData: bytes | bytearray = b"",
        attributes: ResourceAttribute = ResourceAttribute.FILE | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]: ...

    @abstractmethod
    def new_directory(
        self,
        name: str,
        attributes: ResourceAttribute = ResourceAttribute.DIRECTORY | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]: ...

    @abstractmethod
    def export_to(self, folderPath: Path | str) -> bool: ...

    @classmethod
    @abstractmethod
    def import_from(self, path: Path |
                    str) -> Optional["A_ResourceHandle"]: ...

    @abstractmethod
    def read(self, __size: int, /) -> bytes: ...

    @abstractmethod
    def write(self, __buffer: ReadableBuffer, /) -> int: ...

    @abstractmethod
    def seek(self, __offset: int, __whence: int = os.SEEK_CUR) -> int: ...

    def _get_files_by_load_type(self) -> _LoadSortedHandles:
        mramHandles = []
        aramHandles = []
        dvdHandles = []
        for handle in self.get_handles():
            if handle.is_directory():
                info = handle._get_files_by_load_type()
                mramHandles.extend(info.mram)
                aramHandles.extend(info.aram)
                dvdHandles.extend(info.dvd)
            else:
                if handle.is_flagged(ResourceAttribute.PRELOAD_TO_MRAM):
                    mramHandles.append(handle)
                elif handle.is_flagged(ResourceAttribute.PRELOAD_TO_ARAM):
                    mramHandles.append(handle)
                elif handle.is_flagged(ResourceAttribute.LOAD_FROM_DVD):
                    mramHandles.append(handle)
                else:
                    raise ValueError(
                        f"Resource handle {handle.get_name()} isn't set to load")
        return A_ResourceHandle._LoadSortedHandles(
            mramHandles,
            aramHandles,
            dvdHandles
        )

    def _get_data_info(self, offset: int) -> _DataInformation:
        mramData = b""
        aramData = b""
        dvdData = b""

        offsetMap: dict["A_ResourceHandle", int] = {}

        startOffset = offset
        sortedHandles = self._get_files_by_load_type()
        for handle in sortedHandles.mram:
            offsetMap[handle] = offset
            data = handle.get_data()
            mramData += data
            offset += len(data)

        mramSize = offset - startOffset

        startOffset = offset
        for handle in sortedHandles.aram:
            offsetMap[handle] = offset
            data = handle.get_data()
            aramData += data
            offset += len(data)

        aramSize = offset - startOffset

        startOffset = offset
        for handle in sortedHandles.dvd:
            offsetMap[handle] = offset
            data = handle.get_data()
            dvdData += data
            offset += len(data)

        dvdSize = offset - startOffset

        return A_ResourceHandle._DataInformation(
            data=BytesIO(mramData + aramData + dvdData),
            offsets=offsetMap,
            mramSize=mramSize,
            aramSize=aramSize,
            dvdSize=dvdSize
        )


class ResourceFile(A_ResourceHandle):
    def __init__(
        self,
        name: str,
        initialData: bytes | bytearray = b"",
        parent: Optional["ResourceDirectory"] = None,
        attributes: ResourceAttribute = ResourceAttribute.FILE | ResourceAttribute.PRELOAD_TO_MRAM
    ):
        super().__init__(name, parent, attributes)

        self._data = BytesIO(initialData)
        self._id = -1

    def is_directory(self) -> bool:
        return False

    def is_file(self) -> bool:
        return True

    def get_magic(self) -> str:
        name = self.get_name()
        return name.upper().ljust(4)

    def sync_ids(self) -> bool:
        archive = self.get_archive()
        if archive is None:
            return False
        return archive.sync_ids()

    def get_id(self) -> int:
        return self._id

    def set_id(self, __id: int, /) -> None:
        # parent = self.get_parent()
        # if parent and parent.sync_ids():
        #     return
        self._id = __id

    def get_size(self) -> int:
        return 0

    def get_data(self) -> bytes:
        data = self.get_raw_data()

        fill = 32 - (len(data) % 32)
        if fill == 32:
            return data

        return data + b"\x00" * fill

    def get_raw_data(self) -> bytes:
        return self._data.getvalue()

    def get_handles(self, *, flatten: bool = False) -> list["A_ResourceHandle"]:
        return []

    def get_handle(self, __path: PurePath | str, /) -> Optional["A_ResourceHandle"]:
        return None

    def path_exists(self, path: PurePath | str) -> bool:
        return self.get_handle(path) is not None

    def add_handle(self, __handle: "A_ResourceHandle", /) -> bool:
        return False

    def remove_handle(self, __handle: "A_ResourceHandle", /) -> bool:
        return False

    def remove_path(self, __path: PurePath | str, /) -> bool:
        return False

    def new_file(
        self,
        name: str,
        initialData: bytes | bytearray = b"",
        attributes: ResourceAttribute = ResourceAttribute.FILE | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]:
        return None

    def new_directory(
        self,
        name: str,
        attributes: ResourceAttribute = ResourceAttribute.DIRECTORY | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]:
        return None

    def export_to(self, folderPath: Path | str) -> bool:
        if isinstance(folderPath, str):
            folderPath = Path(folderPath)

        if not folderPath.is_dir():
            return False

        thisFile = folderPath / self.get_name()
        thisFile.write_bytes(
            self.get_data()
        )

        return True

    @classmethod
    def import_from(self, path: Path | str) -> Optional["A_ResourceHandle"]:
        if isinstance(path, str):
            path = Path(path)

        if not path.is_file():
            return None

        return ResourceFile(
            path.name,
            path.read_bytes()
        )

    def read(self, __size: int, /) -> bytes:
        return self._data.read(__size)

    def write(self, __buffer: ReadableBuffer, /) -> int:
        return self._data.write(__buffer)

    def seek(self, __offset: int, __whence: int = os.SEEK_CUR) -> int:
        return self._data.seek(__offset, __whence)


class ResourceDirectory(A_ResourceHandle):
    def __init__(
        self,
        name: str,
        parent: Optional["ResourceDirectory"] = None,
        children: Optional[list["A_ResourceHandle"]] = None,
        attributes: ResourceAttribute = ResourceAttribute.DIRECTORY | ResourceAttribute.PRELOAD_TO_MRAM
    ):
        super().__init__(name, parent, attributes)

        if children is None:
            children = []
        self._children = children

    def is_directory(self) -> bool:
        return True

    def is_file(self) -> bool:
        return False

    def sync_ids(self) -> bool:
        archive = self.get_archive()
        if archive is None:
            return False
        return archive.sync_ids()

    def get_magic(self) -> str:
        name = self.get_name()
        return name.upper().ljust(4)

    def get_id(self) -> int:
        return -1

    def set_id(self, __id: int, /) -> None:
        pass

    def get_size(self) -> int:
        return 0

    def get_data(self) -> bytes:
        return b""

    def get_raw_data(self) -> bytes:
        return b""

    def get_handles(self, *, flatten: bool = False) -> list["A_ResourceHandle"]:
        if not flatten:
            return self._children

        def _get_r_handles(thisHandle: "A_ResourceHandle", handles: list) -> None:
            for handle in thisHandle.get_handles():
                if handle.is_directory():
                    _get_r_handles(handle, handles)
                elif handle.is_file():
                    handles.append(handle)
                else:
                    raise ValueError(
                        f"Handle \"{handle.get_name}\" is not a file nor directory")

        handles: list[A_ResourceHandle] = []
        _get_r_handles(self, handles)

        return handles

    def get_handle(self, __path: PurePath | str, /) -> Optional["A_ResourceHandle"]:
        if isinstance(__path, str):
            __path = PurePath(__path)
        curDir, *subParts = __path.parts
        curDir = str(curDir)
        for handle in self.get_handles():
            if handle.get_name() == curDir:
                if len(subParts) == 0:
                    return handle
                return handle.get_handle(
                    PurePath(*subParts)
                )
        return None

    def path_exists(self, __path: PurePath | str, /) -> bool:
        return self.get_handle(__path) is not None

    def add_handle(self, __handle: "A_ResourceHandle", /) -> bool:
        if __handle in self._children:
            return False

        self._children.append(__handle)
        __handle._parent = self
        return True

    def remove_handle(self, __handle: "A_ResourceHandle", /) -> bool:
        if __handle not in self._children:
            return False

        self._children.remove(__handle)
        return True

    def remove_path(self, __path: PurePath | str, /) -> bool:
        handle = self.get_handle(__path)
        if handle is None:
            return False

        self._children.remove(handle)
        return True

    def new_file(
        self,
        name: str,
        initialData: bytes | bytearray = b"",
        attributes: ResourceAttribute = ResourceAttribute.FILE | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]:
        if self.path_exists(PurePath(name)):
            return None

        newFile = ResourceFile(
            name,
            initialData,
            self,
            attributes
        )

        self._children.append(newFile)
        return newFile

    def new_directory(
        self,
        name: str,
        attributes: ResourceAttribute = ResourceAttribute.DIRECTORY | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]:
        if self.path_exists(PurePath(name)):
            return None

        newDir = ResourceDirectory(
            name,
            self,
            attributes=attributes
        )

        self._children.append(newDir)
        return newDir

    def export_to(self, folderPath: Path | str) -> bool:
        if isinstance(folderPath, str):
            folderPath = Path(folderPath)

        if not folderPath.is_dir():
            return False

        thisDir = folderPath / self.get_name()
        thisDir.mkdir(exist_ok=True)

        successful = True
        for handle in self.get_handles():
            successful &= handle.export_to(thisDir)

        return successful

    @classmethod
    def import_from(self, path: Path | str) -> Optional["A_ResourceHandle"]:
        if isinstance(path, str):
            path = Path(path)

        if not path.is_dir():
            return None

        thisResource = ResourceDirectory(path.name)

        for p in path.iterdir():
            resource: A_ResourceHandle | None
            if p.is_dir():
                resource = ResourceDirectory.import_from(p)
            elif p.is_file():
                resource = ResourceFile.import_from(p)

            if resource is None:
                continue

            thisResource._children.append(resource)

        return thisResource

    def read(self, __size: int, /) -> bytes:
        raise RuntimeError("Resource directories don't have read support")

    def write(self, __buffer: ReadableBuffer, /) -> int:
        raise RuntimeError("Resource directories don't have write support")

    def seek(self, __offset: int, __whence: int = os.SEEK_CUR) -> int:
        return 0


class ResourceArchive(ResourceDirectory, A_Serializable):
    @dataclass
    class FileEntry:
        fileID: int
        flags: int
        name: str
        offset: int
        size: int
        nameHash: int

    @dataclass
    class DirectoryEntry:
        magic: str
        name: str
        nameOffset: int
        nameHash: int
        fileCount: int
        firstFileOffset: int

    @dataclass
    class _StringTableData:
        strings: bytes
        offsets: dict[str, int]

    def __init__(
        self,
        rootName: str,
        children: Optional[list["A_ResourceHandle"]] = None,
        syncIDs: bool = True
    ):
        super().__init__(rootName, None, children)

        if children is None:
            children = []
        self._children = children
        self._syncIDs = syncIDs

    @classmethod
    def from_bytes(cls, data: BinaryIO, *args: VariadicArgs, **kwargs: VariadicKwargs) -> Optional["ResourceArchive"]:
        assert data.read(4) == b"RARC", "Invalid identifier. Expected \"RARC\""

        archive = ResourceArchive("Root")

        # Header
        rarcSize = read_uint32(data)
        dataHeaderOffset = read_uint32(data)
        dataOffset = read_uint32(data) + 0x20
        dataLength = read_uint32(data)
        mramSize = read_uint32(data)
        aramSize = read_uint32(data)
        data.seek(4, 1)

        # Data Header
        directoryCount = read_uint32(data)
        directoryTableOffset = read_uint32(data) + 0x20
        fileEntryCount = read_uint32(data)
        fileEntryTableOffset = read_uint32(data) + 0x20
        stringTableSize = read_uint32(data)
        stringTableOffset = read_uint32(data) + 0x20
        nextFreeFileID = read_uint16(data)
        syncIDs = read_bool(data)

        archive._syncIDs = syncIDs

        # Directory Nodes
        data.seek(directoryTableOffset, 0)

        flatDirectoryList: list[ResourceArchive.DirectoryEntry] = []
        for _ in range(directoryCount):
            magic = read_string(data, maxlen=3)
            nameOffset = read_uint32(data)
            nameHash = read_uint16(data)
            fileCount = read_uint16(data)
            firstFileOffset = read_uint32(data)

            _oldPos = data.tell()
            data.seek(stringTableOffset + nameOffset, 0)
            name = read_string(data)
            data.seek(_oldPos, 0)

            flatDirectoryList.append(
                ResourceArchive.DirectoryEntry(
                    magic,
                    name,
                    nameOffset,
                    nameHash,
                    fileCount,
                    firstFileOffset
                )
            )

        # File Nodes
        data.seek(fileEntryTableOffset, 0)

        flatFileList: list[ResourceArchive.FileEntry] = []
        for _ in range(fileEntryCount):
            entryID = read_sint16(data)
            nameHash = read_sint16(data)
            type_ = read_sint16(data)

            nameOffset = read_uint16(data)
            offset = read_sint32(data)
            size = read_sint32(data)

            data.seek(4, 1)

            _oldPos = data.tell()
            data.seek(stringTableOffset + nameOffset, 0)
            name = read_string(data)
            data.seek(_oldPos, 0)

            flatFileList.append(
                ResourceArchive.FileEntry(
                    entryID,
                    type_,
                    name,
                    offset,
                    size,
                    nameHash
                )
            )

        # Directory Construction
        directories: list[ResourceDirectory] = []

        dirToFileEntry: dict[ResourceDirectory,
                             list[ResourceArchive.FileEntry]] = {}
        for i, dirEntry in enumerate(flatDirectoryList):
            directory: ResourceDirectory
            if i == 0:
                directory = archive
                directory.set_name(dirEntry.name)
            else:
                directory = ResourceDirectory(dirEntry.name)
            fileEntries = dirToFileEntry.setdefault(directory, [])
            for handleEntry in flatFileList[dirEntry.firstFileOffset:dirEntry.fileCount + dirEntry.firstFileOffset]:
                if handleEntry.flags == 0x200:
                    fileEntries.append(handleEntry)
                else:
                    data.seek(dataOffset + handleEntry.offset, 0)
                    directory.add_handle(
                        ResourceFile(
                            handleEntry.name,
                            initialData=data.read(handleEntry.size),
                            attributes=ResourceAttribute(
                                handleEntry.flags >> 8)
                        )
                    )
            directories.append(directory)

        for directory in directories:
            for subdir in dirToFileEntry[directory]:
                refDir = directories[subdir.offset]
                if (subdir.name == "."):  # This Dir
                    # if (subdir.offset == 0):  # Root folder
                    #     archive.set_name(refDir.get_name())
                    continue
                if (subdir.name == ".."):
                    if (subdir.offset == -1 or subdir.offset > len(directories)):
                        continue
                    directory.set_parent(refDir)
                    continue
                if not refDir.get_name() == subdir.name:
                    refDir.set_name(subdir.name)

        return archive

    def to_bytes(self) -> bytes:
        stream = BytesIO()

        dataInfo = self._get_data_info(0)

        flatFileList = self._get_file_entry_list(dataInfo)
        flatDirectoryList = self._get_flat_directory_list()

        stringTableData = self._get_string_table_data(flatFileList)

        # File Writing
        stream.write(b"RARC")
        stream.write(
            b"\xDD\xDD\xDD\xDD\x00\x00\x00\x20\xDD\xDD\xDD\xDD\xEE\xEE\xEE\xEE")
        write_uint32(stream, dataInfo.mramSize)
        write_uint32(stream, dataInfo.aramSize)
        write_uint32(stream, dataInfo.dvdSize)

        # Data Header
        write_uint32(stream, len(flatDirectoryList))
        stream.write(b"\xDD\xDD\xDD\xDD")
        write_uint32(stream, len(flatFileList))
        stream.write(b"\xDD\xDD\xDD\xDD")
        stream.write(b"\xEE\xEE\xEE\xEE")
        stream.write(b"\xEE\xEE\xEE\xEE")
        write_uint16(stream, len(flatFileList))
        write_bool(stream, self.sync_ids())

        # Padding
        stream.write(b"\x00\x00\x00\x00\x00")

        # Directory Nodes
        directoryEntryOffset = stream.tell()

        for directory in flatDirectoryList:
            stream.write(directory.magic.encode())
            write_uint32(stream, stringTableData.offsets[directory.name])
            write_uint16(stream, directory.nameHash)
            write_uint16(stream, directory.fileCount)
            write_uint32(stream, directory.firstFileOffset)

        # Padding
        write_pad32(stream)

        # File Entries
        fileEntryOffset = stream.tell()
        for entry in flatFileList:
            write_sint16(stream, entry.fileID)
            write_uint16(stream, entry.nameHash)
            write_uint16(stream, entry.flags)
            write_uint16(stream, stringTableData.offsets[entry.name])
            write_sint32(stream, entry.offset)
            write_sint32(stream, entry.size)
            stream.write(b"\x00\x00\x00\x00")

        # Padding
        write_pad32(stream)

        # String Table
        stringTableOffset = stream.tell()
        stream.write(stringTableData.strings)

        # Padding
        write_pad32(stream)

        # File Table
        fileTableOffset = stream.tell()
        stream.write(dataInfo.data.getvalue())

        # Header
        rarcSize = len(stream.getvalue())

        stream.seek(0x4, 0)
        write_uint32(stream, rarcSize)
        stream.seek(0x4, 1)
        write_uint32(stream, fileTableOffset - 0x20)
        write_uint32(stream, rarcSize - fileTableOffset)
        stream.seek(0x10, 1)
        write_uint32(stream, directoryEntryOffset - 0x20)
        stream.seek(0x4, 1)
        write_uint32(stream, fileEntryOffset - 0x20)
        write_uint32(stream, fileTableOffset - stringTableOffset)
        write_uint32(stream, stringTableOffset - 0x20)

        return stream.getvalue()

    def path_exists(self, path: PurePath | str) -> bool:
        return self.get_handle(path) is not None

    def new_file(
        self,
        name: str,
        initialData: bytes | BinaryIO = b"",
        attributes: ResourceAttribute = ResourceAttribute.FILE | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]:
        return None

    def new_directory(
        self,
        name: str,
        attributes: ResourceAttribute = ResourceAttribute.DIRECTORY | ResourceAttribute.PRELOAD_TO_MRAM
    ) -> Optional["A_ResourceHandle"]:
        return None

    @classmethod
    def import_from(self, path: Path | str) -> Optional["A_ResourceHandle"]:
        if isinstance(path, str):
            path = Path(path)

        if not path.is_dir():
            return None

        thisResource = ResourceArchive(path.name)

        for p in path.iterdir():
            resource: A_ResourceHandle | None
            if p.is_dir():
                resource = ResourceDirectory.import_from(p)
            elif p.is_file():
                resource = ResourceFile.import_from(p)

            if resource is None:
                continue

            thisResource._children.append(resource)

        return thisResource

    def sync_ids(self) -> bool:
        return self._syncIDs

    def get_magic(self) -> str:
        return "ROOT"

    def get_next_free_id(self) -> int:
        allIDs: list[int] = []
        for handle in self.get_handles(flatten=True):
            if handle.is_directory():
                continue
            allIDs.append(handle.get_id())

        if len(allIDs) == 0:
            return 0

        allIDs.sort()
        for i in range(allIDs[0] + 1, allIDs[-1]):
            if i not in allIDs:
                return i
        return len(allIDs)

    def _get_file_entry_list(self, dataInfo: "ResourceArchive._DataInformation") -> list["ResourceArchive.FileEntry"]:
        globalID = 0
        nextFolderID = 1

        def _process_dir(dir: A_ResourceHandle, currentFolderID: int, backwardsFolderID: int) -> list["ResourceArchive.FileEntry"]:
            nonlocal globalID
            nonlocal nextFolderID

            fileList: list[ResourceArchive.FileEntry] = []
            directories: dict[int, A_ResourceHandle] = {}

            for handle in dir.get_handles():
                if handle.is_file():
                    fileList.append(
                        ResourceArchive.FileEntry(
                            fileID=globalID if dir.sync_ids() else handle.get_id(),
                            flags=handle._attributes << 8,
                            name=handle.get_name(),
                            offset=dataInfo.offsets[handle],
                            size=len(handle.get_data()),
                            nameHash=jdrama.get_key_code(handle.get_name())
                        )
                    )
                else:
                    directories[len(fileList)] = handle
                    fileList.append(
                        ResourceArchive.FileEntry(
                            fileID=-1,
                            flags=0x200,
                            name=handle.get_name(),
                            offset=nextFolderID,
                            size=0x10,
                            nameHash=jdrama.get_key_code(handle.get_name())
                        )
                    )
                    nextFolderID += 1
                globalID += 1

            fileList.append(
                ResourceArchive.FileEntry(
                    fileID=-1,
                    flags=0x200,
                    name=".",
                    offset=currentFolderID,
                    size=0x10,
                    nameHash=jdrama.get_key_code(".")
                )
            )
            fileList.append(
                ResourceArchive.FileEntry(
                    fileID=-1,
                    flags=0x200,
                    name="..",
                    offset=backwardsFolderID,
                    size=0x10,
                    nameHash=jdrama.get_key_code("..")
                )
            )
            for size, directory in directories.items():
                fileList.extend(
                    _process_dir(
                        directory, fileList[size].offset, currentFolderID)
                )
            return fileList

        return _process_dir(self, 0, -1)

    def _get_flat_directory_list(self) -> list["ResourceArchive.DirectoryEntry"]:
        firstFileOffset = 0

        def _process_dir(dir: A_ResourceHandle) -> list["ResourceArchive.DirectoryEntry"]:
            nonlocal firstFileOffset

            dirList: list[ResourceArchive.DirectoryEntry] = []
            tmpList: list[ResourceArchive.DirectoryEntry] = []

            firstFileOffset += len(dir.get_handles()) + 2
            for handle in dir.get_handles():
                if handle.is_directory():
                    dirList.append(
                        ResourceArchive.DirectoryEntry(
                            magic=handle.get_magic(),
                            name=handle.get_name(),
                            nameOffset=-1,
                            nameHash=jdrama.get_key_code(handle.get_name()),
                            fileCount=len(handle.get_handles()),
                            firstFileOffset=firstFileOffset
                        )
                    )
                    tmpList.extend(
                        _process_dir(handle)
                    )
            dirList.extend(tmpList)
            return dirList

        return _process_dir(self)

    def _get_string_table_data(self, fileList: list["ResourceArchive.FileEntry"]) -> _StringTableData:
        offsets: dict[str, int] = {}
        stringBuf = BytesIO()

        rootName = self.get_name()
        offsets[rootName] = 0
        write_string(stringBuf, rootName)

        offsets["."] = stringBuf.tell()
        write_string(stringBuf, ".")

        offsets[".."] = stringBuf.tell()
        write_string(stringBuf, "..")

        for entry in fileList:
            if entry.name not in offsets:
                offsets[entry.name] = stringBuf.tell()
                write_string(stringBuf, entry.name)

        return ResourceArchive._StringTableData(
            stringBuf.getvalue(),
            offsets
        )
