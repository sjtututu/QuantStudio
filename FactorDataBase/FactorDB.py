# coding=utf-8
import time
import os
import uuid
import shutil
import mmap
import pickle
import gc
import shelve
import datetime as dt
from multiprocessing import Process, Queue, Lock, Event

import numpy as np
import pandas as pd
from tqdm import tqdm
from progressbar import ProgressBar
from traits.api import Instance, Str, File, List, Int, Bool, Directory, Enum, ListStr

from QuantStudio import __QS_Object__, __QS_Error__, __QS_CachePath__, __QS_CacheLock__
from QuantStudio.Tools.IDFun import testIDFilterStr
from QuantStudio.Tools.AuxiliaryFun import genAvailableName, partitionList, startMultiProcess
from QuantStudio.Tools.FileFun import listDirDir

def _adjustDateTime(data, dts=None, fillna=False, **kwargs):
    if isinstance(data, (pd.DataFrame, pd.Series)):
        if dts is not None:
            if fillna:
                AllDTs = data.index.union(set(dts))
                AllDTs = AllDTs.sort_values()
                data = data.ix[AllDTs]
                data = data.fillna(**kwargs)
            data = data.ix[dts]
    else:
        if dts is not None:
            FactorNames = data.items
            if fillna:
                AllDTs = data.major_axis.union(set(dts))
                AllDTs = AllDTs.sort_values()
                data = data.ix[:, AllDTs, :]
                data = pd.Panel({data.items[i]:data.iloc[i].fillna(axis=0, **kwargs) for i in range(data.shape[0])})
            data = data.ix[FactorNames, dts, :]
    return data

# 因子库, 只读, 接口类
# 数据库由若干张因子表组成
# 不支持某个操作时, 方法产生错误
# 没有相关数据时, 方法返回 None
class FactorDB(__QS_Object__):
    """因子库"""
    Name = Str("因子库")
    # ------------------------------数据源操作---------------------------------
    # 链接到数据库
    def connect(self):
        return 0
    # 断开到数据库的链接
    def disconnect(self):
        return 0
    # 检查数据库是否可用
    def isAvailable(self):
        return True
    # -------------------------------表的操作---------------------------------
    # 表名, 返回: array([表名])
    @property
    def TableNames(self):
        return []
    # 返回因子表对象
    def getTable(self, table_name, args={}):
        return None

# 支持写入的因子库, 接口类
class WritableFactorDB(FactorDB):
    """可写入的因子数据库"""
    # -------------------------------表的操作---------------------------------
    # 重命名表. 必须具体化
    def renameTable(self, old_table_name, new_table_name):
        return 0
    # 删除表. 必须具体化
    def deleteTable(self, table_name):
        return 0
    # 设置表的元数据. 必须具体化
    def setTableMetaData(self, table_name, key=None, value=None, meta_data=None):
        return 0
    # --------------------------------因子操作-----------------------------------
    # 对一张表的因子进行重命名. 必须具体化
    def renameFactor(self, table_name, old_factor_name, new_factor_name):
        return 0
    # 删除一张表中的某些因子. 必须具体化
    def deleteFactor(self, table_name, factor_names):
        return 0
    # 设置因子的元数据. 必须具体化
    def setFactorMetaData(self, table_name, ifactor_name, key=None, value=None, meta_data=None):
        return 0
    # 写入数据, if_exists: append, update, replace, skip. 必须具体化
    def writeData(self, data, table_name, if_exists='append', **kwargs):
        return 0
    # -------------------------------数据变换------------------------------------
    # 复制因子, 并不删除原来的因子
    def copyFactor(self, target_table, table_name, factor_names=None, if_exists='append', args={}):
        FT = self.getTable(table_name)
        if factor_names is None:
            factor_names = FT.FactorNames
        Data = FT.readData(factor_names=factor_names, args=args)
        return self.writeData(Data, target_table, if_exists=if_exists)
    # 时间平移, 沿着时间轴将所有数据纵向移动 lag 期, lag>0 向前移动, lag<0 向后移动, 空出来的地方填 nan
    def offsetDateTime(self, lag, table_name, factor_names=None, args={}):
        if lag==0:
            return 0
        FT = self.getTable(table_name)
        Data = FT.readData(factor_names=factor_names, args=args)
        if lag>0:
            Data.iloc[:,lag:,:] = Data.iloc[:,:-lag,:].values
            Data.iloc[:,:lag,:] = None
        elif lag<0:
            Data.iloc[:,:lag,:] = Data.iloc[:,-lag:,:].values
            Data.iloc[:,:lag,:] = None
        self.writeData(Data, table_name, if_exists='replace')
        return 0
    # 数据变换, 对原来的时间和ID序列通过某种变换函数得到新的时间序列和ID序列, 调整数据
    def changeData(self, table_name, factor_names=None, ids=None, dts=None, args={}):
        if dts is None:
            return 0
        Data = self.getTable(table_name).readData(factor_names=factor_names, ids=ids, dts=dts, args=args)
        self.writeData(Data, table_name, if_exists='replace')
        return 0
    # 填充缺失值
    def fillNA(self, filled_value, table_name, factor_names=None, ids=None, dts=None, args={}):
        Data = self.getTable(table_name).readData(factor_names=factor_names, ids=ids, dts=dts, args=args)
        Data.fillna(filled_value, inplace=True)
        self.writeData(Data, table_name, if_exists='update')
        return 0
    # 替换数据
    def replaceData(self, old_value, new_value, table_name, factor_names=None, ids=None, dts=None, args={}):
        Data = self.getTable(table_name).readData(factor_names=factor_names, ids=ids, dts=dts, args=args)
        Data = Data.where(Data!=old_value, new_value)
        self.writeData(Data, table_name, if_exists='update')
        return 0
    # 压缩数据
    def compressData(self, table_name=None, factor_names=None):
        return 0

# 因子表的遍历模式参数对象
class _ErgodicMode(__QS_Object__):
    """遍历模式"""
    ForwardPeriod = Int(600, arg_type="Integer", label="向前缓冲时点数", order=0)
    BackwardPeriod = Int(1, arg_type="Integer", label="向后缓冲时点数", order=1)
    CacheMode = Enum("因子", "ID", arg_type="SingleOption", label="缓冲模式", order=2)
    MaxFactorCacheNum = Int(60, arg_type="Integer", label="最大缓冲因子数", order=3)
    MaxIDCacheNum = Int(10000, arg_type="Integer", label="最大缓冲ID数", order=4)
    def __init__(self, sys_args={}, **kwargs):
        super().__init__(sys_args=sys_args, **kwargs)
        self._isStarted = False
    def __getstate__(self):
        state = self.__dict__.copy()
        if "_CacheDataProcess" in state: state["_CacheDataProcess"] = None
        return state
# 基于 mmap 的缓冲数据, 如果开启遍历模式, 那么限制缓冲的因子个数, ID 个数, 时间点长度, 缓冲区里是因子的部分数据
def _prepareMMAPFactorCacheData(ft):
    CacheData, CacheDTs, Msg, MMAPCacheData, DTNum = {}, [], None, None, len(ft.ErgodicMode._DateTimes)
    while True:
        Task = ft.ErgodicMode._Queue2SubProcess.get()# 获取任务
        if Task is None: break# 结束进程
        if (Task[0] is None) and (Task[1] is None):# 把数据装入缓冲区
            CacheDataByte = pickle.dumps(CacheData)
            DataLen = len(CacheDataByte)
            if os.name=='nt': MMAPCacheData = mmap.mmap(-1, DataLen, tagname=ft.ErgodicMode._TagName)
            else: Msg = MMAPCacheData = mmap.mmap(-1, DataLen)
            MMAPCacheData.seek(0)
            MMAPCacheData.write(CacheDataByte)
            CacheDataByte = None
            ft.ErgodicMode._Queue2MainProcess.put((DataLen, Msg))
            gc.collect()
        elif Task[0] is None:# 调整缓存区数据
            NewFactors, PopFactors = Task[1]
            for iFactorName in PopFactors: CacheData.pop(iFactorName)
            if NewFactors: CacheData.update(dict(ft.__QS_calcData__(raw_data=ft.__QS_prepareRawData__(factor_names=NewFactors, ids=ft.ErgodicMode._IDs, dts=CacheDTs), factor_names=NewFactors, ids=ft.ErgodicMode._IDs, dts=CacheDTs)))
        else:# 准备缓冲区
            Msg = MMAPCacheData = None# 这句话必须保留...诡异
            CurInd = Task[0] + ft.ErgodicMode.ForwardPeriod + 1
            if CurInd < DTNum:# 未到结尾处, 需要再准备缓存数据
                OldCacheDTs = set(CacheDTs)
                CacheDTs = ft.ErgodicMode._DateTimes[max((0, CurInd-ft.ErgodicMode.BackwardPeriod)):min((DTNum, CurInd+ft.ErgodicMode.ForwardPeriod+1))].tolist()
                NewCacheDTs = sorted(set(CacheDTs).difference(OldCacheDTs))
                if CacheData:
                    CacheFactorNames = list(CacheData.keys())
                    NewCacheData = ft.__QS_calcData__(raw_data=ft.__QS_prepareRawData__(factor_names=CacheFactorNames, ids=ft.ErgodicMode._IDs, dts=NewCacheDTs), factor_names=CacheFactorNames, ids=ft.ErgodicMode._IDs, dts=NewCacheDTs)
                    for iFactorName in CacheData:
                        CacheData[iFactorName] = CacheData[iFactorName].ix[CacheDTs, :]
                        CacheData[iFactorName].ix[NewCacheDTs, :] = NewCacheData[iFactorName]
                    NewCacheData = None
    return 0
# 基于 mmap 的 ID 缓冲的因子表, 如果开启遍历模式, 那么限制缓冲的 ID 个数和时间点长度, 缓冲区里是 ID 的部分数据
def _prepareMMAPIDCacheData(ft):
    CacheData, CacheDTs, Msg, MMAPCacheData, DTNum = {}, [], None, None, len(ft.ErgodicMode._DateTimes)
    while True:
        Task = ft.ErgodicMode._Queue2SubProcess.get()# 获取任务
        if Task is None: break# 结束进程
        if (Task[0] is None) and (Task[1] is None):# 把数据装入缓冲区
            CacheDataByte = pickle.dumps(CacheData)
            DataLen = len(CacheDataByte)
            if os.name=='nt': MMAPCacheData = mmap.mmap(-1, DataLen, tagname=ft.ErgodicMode._TagName)
            else: Msg = MMAPCacheData = mmap.mmap(-1, DataLen)
            MMAPCacheData.seek(0)
            MMAPCacheData.write(CacheDataByte)
            CacheDataByte = None
            ft.ErgodicMode._Queue2MainProcess.put((DataLen, Msg))
            gc.collect()
        elif Task[0] is None:# 调整缓存区数据
            NewID, PopID = Task[1]
            if PopID: CacheData.pop(PopID)# 用新 ID 数据替换旧 ID
            if NewID: CacheData[NewID] = ft.__QS_calcData__(raw_data=ft.__QS_prepareRawData__(factor_names=ft.FactorNames, ids=[NewID], dts=CacheDTs), factor_names=ft.FactorNames, ids=[NewID], dts=CacheDTs).iloc[:, :, 0]
        else:# 准备缓冲区
            Msg = MMAPCacheData = None# 这句话必须保留...诡异
            CurInd = Task[0] + ft.ErgodicMode.ForwardPeriod + 1
            if CurInd<DTNum:# 未到结尾处, 需要再准备缓存数据
                OldCacheDTs = set(CacheDTs)
                CacheDTs = ft.ErgodicMode._DateTimes[max((0, CurInd-ft.ErgodicMode.BackwardPeriod)):min((DTNum, CurInd+ft.ErgodicMode.ForwardPeriod+1))].tolist()
                NewCacheDTs = sorted(set(CacheDTs).difference(OldCacheDTs))
                if CacheData:
                    CacheIDs = list(CacheData.keys())
                    NewCacheData = ft.__QS_calcData__(raw_data=ft.__QS_prepareRawData__(factor_names=ft.FactorNames, ids=CacheIDs, dts=NewCacheDTs), factor_names=ft.FactorNames, ids=CacheIDs, dts=NewCacheDTs)
                    for iID in CacheData:
                        CacheData[iID] = CacheData[iID].ix[CacheDTs, :]
                        CacheData[iID].ix[NewCacheDTs, :] = NewCacheData.loc[:, :, iID]
                    NewCacheData = None
    return 0
# 因子表的运算模式参数对象
class _OperationMode(__QS_Object__):
    """运算模式"""
    DateTimes = List(dt.datetime, arg_type="DateTimeList", label="运算时点", order=0)
    IDs = List(str, arg_type="IDList", label="运算ID", order=1)
    FactorNames = ListStr(arg_type="MultiOption", label="运算因子", order=2, option_range=())
    SubProcessNum = Int(0, arg_type="Integer", label="子进程数", order=3)
    DTRuler = List(dt.datetime, arg_type="DateTimeList", label="时点标尺", order=4)
    def __init__(self, ft, sys_args={}, **kwargs):
        self._FT = ft
        self._isStarted = False
        self._Factors = []# 因子列表
        self._FactorDict = {}# 因子字典, {因子名:因子}, 包括所有的因子, 即衍生因子所依赖的描述子也在内
        self._FactorStartDT = {}# {因子名: 起始时点}
        self._iPID = "0"# 对象所在的进程 ID
        self._PIDs = []# 所有的计算进程 ID, 单进程下默认为"0", 多进程为"0-i"
        self._PID_IDs = {}# 每个计算进程分配的 ID 列表, {PID:[ID]}
        self._PID_Lock = {}# 每个计算进程分配的缓存数据锁, {PID:Lock}
        self._RawDataDir = ""# 原始数据存放根目录
        self._CacheDataDir = "  "# 中间数据存放根目录
        self._Event = {}# {因子名: (Sub2MainQueue, Event)}
        self._FileSuffix = (".dat" if os.name=="nt" else "")
        super().__init__(sys_args=sys_args, **kwargs)
    def __QS_initArgs__(self):
        self.add_trait("FactorNames", ListStr(arg_type="MultiOption", label="运算因子", order=2, option_range=tuple(self._FT.FactorNames)))
# 因子表准备子进程
def _prepareRawData(args):
    if "Sub2MainQueue" not in args:# 运行模式为串行
        for i in tqdm(range(len(args['GroupInfo']))):
            iFT, iFactorNames, iRawFactorNames, iDTs, iArgs = args['GroupInfo'][i]
            iRawData = iFT.__QS_prepareRawData__(iRawFactorNames, args["FT"].OperationMode.IDs, iDTs, iArgs)
            iFT.__QS_saveRawData__(iRawData, iRawFactorNames, args["FT"].OperationMode._RawDataDir, args["FT"].OperationMode._PID_IDs, args["RawDataFileNames"][i])
    else:# 运行模式为并行
        for i in range(len(args['GroupInfo'])):
            iFT, iFactorNames, iRawFactorNames, iDTs, iArgs = args['GroupInfo'][i]
            iRawData = iFT.__QS_prepareRawData__(iRawFactorNames, args['FT'].OperationMode.IDs, iDTs, iArgs)
            iFT.__QS_saveRawData__(iRawData, iRawFactorNames, args["FT"].OperationMode._RawDataDir, args["FT"].OperationMode._PID_IDs, args["RawDataFileNames"][i])
            args['Sub2MainQueue'].put((args["PID"], 1, None))
    return 0
# 因子表运算子进程
def _calculate(args):
    FT = args["FT"]
    FT.OperationMode._iPID = args["PID"]
    if FT.OperationMode.SubProcessNum==0:# 运行模式为串行
        nTask = len(FT.OperationMode.FactorNames)
        with ProgressBar(max_value=nTask) as ProgBar:
            for i, iFactor in enumerate(FT.OperationMode._Factors):
                iData = iFactor._QS_getData(dts=FT.OperationMode.DateTimes, pids=[args["PID"]])
                args["FactorDB"].writeData(pd.Panel({iFactor.Name:iData}), args["TableName"], if_exists=args["if_exists"])
                iData = None
                ProgBar.update(i+1)
    else:
        for i, iFactor in enumerate(FT.OperationMode._Factors):
            iData = iFactor._QS_getData(dts=FT.OperationMode.DateTimes, pids=[args["PID"]])
            args["FactorDB"].writeData(pd.Panel({iFactor.Name:iData}), args["TableName"], if_exists=args["if_exists"])
            iData = None
            args['Sub2MainQueue'].put((args["PID"], 1, None))
    return 0
# 因子表, 接口类
# 因子表可看做一个独立的数据集或命名空间, 可看做 Panel(items=[因子], major_axis=[时间点], minor_axis=[ID])
# 因子表的数据有三个维度: 时间点, ID, 因子
# 时间点数据类型是 datetime.datetime, ID 和因子名称的数据类型是 str
# 不支持某个操作时, 方法产生错误
# 没有相关数据时, 方法返回 None
class FactorTable(__QS_Object__):
    ErgodicMode = Instance(_ErgodicMode, arg_type="ArgObject", label="遍历模式", order=0)
    OperationMode = Instance(_OperationMode, arg_type="ArgObject", label="运算模式", order=1)
    def __init__(self, name, fdb=None, sys_args={}, **kwargs):
        self._Name = name
        self._FactorDB = fdb# 因子表所属的因子库, None 表示自定义的因子表
        return super().__init__(sys_args=sys_args, **kwargs)
    def __QS_initArgs__(self):
        self.ErgodicMode = _ErgodicMode()
        self.OperationMode = _OperationMode(ft=self)
    @property
    def Name(self):
        return self._Name
    @property
    def FactorDB(self):
        return self._FactorDB
    # -------------------------------表的信息---------------------------------
    # 获取表的元数据
    def getMetaData(self, key=None):
        if key is None: return {}
        return None
    # -------------------------------维度信息-----------------------------------
    # 返回所有因子名
    @property
    def FactorNames(self):
        return []
    # 获取因子对象
    def getFactor(self, ifactor_name, args={}):
        iFactor = Factor(name=ifactor_name, ft=self)
        for iArgName in self.ArgNames:
            if iArgName not in ("遍历模式", "运算模式"):
                iTraitName, iTrait = self.getTrait(iArgName)
                iFactor.add_trait(iTraitName, iTrait)
                iFactor[iArgName] = args.get(iArgName, self[iArgName])
        return iFactor
    # 获取因子的元数据
    def getFactorMetaData(self, factor_names=None, key=None):
        if factor_names is None: factor_names = self.FactorNames
        if key is None: return pd.DataFrame(index=factor_names, dtype=np.dtype("O"))
        else: return pd.Series([None]*len(factor_names), index=factor_names, dtype=np.dtype("O"))
    # 获取 ID 序列
    def getID(self, ifactor_name=None, idt=None, args={}):
        return []
    # 获取 ID 的 Mask, 返回: Series(True or False, index=[ID])
    def getIDMask(self, idt, ids=None, id_filter_str=None, args={}):
        if ids is None: ids = self.getID(idt=idt, args=args)
        if not id_filter_str: return pd.Series(True, index=ids)
        CompiledIDFilterStr, IDFilterFactors = testIDFilterStr(id_filter_str, self.FactorNames)
        if CompiledIDFilterStr is None: raise __QS_Error__("过滤条件字符串有误!")
        temp = self.readData(factor_names=IDFilterFactors, ids=ids, dts=[idt], args=args).loc[:, idt, :]
        return eval(CompiledIDFilterStr)
    # 获取过滤后的ID
    def getFilteredID(self, idt, id_filter_str=None, args={}):
        if not id_filter_str: return self.getID(idt=idt, args=args)
        CompiledIDFilterStr, IDFilterFactors = testIDFilterStr(id_filter_str, self.FactorNames)
        if CompiledIDFilterStr is None: raise __QS_Error__("过滤条件字符串有误!")
        temp = self.readData(factor_names=IDFilterFactors, ids=ids, dts=[idt], args=args).loc[:, idt, :]
        return eval("temp["+CompiledIDFilterStr+"].index.tolist()")
    # 获取时间点序列
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        return []
    # -------------------------------读取数据---------------------------------
    # 准备原始数据的接口
    def __QS_prepareRawData__(self, factor_names=None, ids=None, dts=None, args={}):
        return None
    # 计算数据的接口, 返回: Panel(item=[因子], major_axis=[时间点], minor_axis=[ID])
    def __QS_calcData__(self, raw_data, factor_names=None, ids=None, dts=None, args={}):
        return None
    # 读取数据, 返回: Panel(item=[因子], major_axis=[时间点], minor_axis=[ID])
    def readData(self, factor_names=None, ids=None, dts=None, args={}):
        if self.ErgodicMode._isStarted: return self._readData_ErgodicMode(factor_names=factor_names, ids=ids, dts=dts, args=args)
        return self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=factor_names, ids=ids, dts=dts, args=args), factor_names=factor_names, ids=ids, dts=dts, args=args)
    # ------------------------------------遍历模式------------------------------------
    def _readData_FactorCacheMode(self, factor_names=None, ids=None, dts=None, args={}):
        if factor_names is None: factor_names = self.FactorNames
        self.ErgodicMode._FactorReadNum[factor_names] += 1
        if (self.ErgodicMode.MaxFactorCacheNum<=0) or (not self.ErgodicMode._CacheDTs) or ((self.ErgodicMode._DateTimes[0] if dts is None else dts[0]) < self.ErgodicMode._CacheDTs[0]) or ((self.ErgodicMode._DateTimes[-1] if dts is None else dts[-1]) >self.ErgodicMode._CacheDTs[-1]):
            return self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=factor_names, ids=ids, dts=dts, args=args), factor_names=factor_names, ids=ids, dts=dts, args=args)
        Data = {}
        DataFactorNames = []
        CacheFactorNames = []
        PopFactorNames = []
        for iFactorName in factor_names:
            iFactorData = self.ErgodicMode._CacheData.get(iFactorName)
            if iFactorData is None:# 尚未进入缓存
                if self.ErgodicMode._CacheFactorNum<self.ErgodicMode.MaxFactorCacheNum:# 当前缓存因子数小于最大缓存因子数，那么将该因子数据读入缓存
                    self.ErgodicMode._CacheFactorNum += 1
                    CacheFactorNames.append(iFactorName)
                else:# 当前缓存因子数等于最大缓存因子数，那么将检查最小读取次数的因子
                    CacheFactorReadNum = self.ErgodicMode._FactorReadNum[self.ErgodicMode._CacheData.keys()]
                    MinReadNumInd = CacheFactorReadNum.argmin()
                    if CacheFactorReadNum.loc[MinReadNumInd]<self.ErgodicMode._FactorReadNum[ifactor_name]:# 当前读取的因子的读取次数超过了缓存因子读取次数的最小值，缓存该因子数据
                        CacheFactorNames.append(iFactorName)
                        PopFactor = MinReadNumInd
                        self.ErgodicMode._CacheData.pop(PopFactor)
                        PopFactorNames.append(PopFactor)
                    else:
                        DataFactorNames.append(iFactorName)
            else:
                Data[iFactorName] = iFactorData
        if CacheFactorNames:
            iData = dict(self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=CacheFactorNames, ids=self.ErgodicMode._IDs, dts=self.ErgodicMode._CacheDTs, args=args), factor_names=CacheFactorNames, ids=self.ErgodicMode._IDs, dts=self.ErgodicMode._CacheDTs, args=args))
            Data.update(iData)
            self.ErgodicMode._CacheData.update(iData)
        self.ErgodicMode._Queue2SubProcess.put((None, (CacheFactorNames, PopFactorNames)))
        Data = pd.Panel(Data)
        if Data.shape[0]>0:
            if ids is None: ids = self.ErgodicMode._IDs
            if dts is not None: Data = Data.ix[:, dts, ids]
            else: Data = Data.ix[:, :, ids]
        if not DataFactorNames: return Data
        return self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=DataFactorNames, ids=ids, dts=dts, args=args), factor_names=DataFactorNames, ids=ids, dts=dts, args=args).join(Data)
    def _readIDData(self, iid, factor_names=None, dts=None, args={}):
        self.ErgodicMode._IDReadNum[iid] = self.ErgodicMode._IDReadNum.get(iid, 0) + 1
        if (self.ErgodicMode.MaxIDCacheNum<=0) or (not self.ErgodicMode._CacheDTs) or ((self.ErgodicMode._DateTimes[0] if dts is None else dts[0]) < self.ErgodicMode._CacheDTs[0]) or ((self.ErgodicMode._DateTimes[-1] if dts is None else dts[-1]) >self.ErgodicMode._CacheDTs[-1]):
            return self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=factor_names, ids=[iid], dts=dts, args=args), factor_names=factor_names, ids=[iid], dts=dts, args=args).iloc[:, :, 0]
        IDData = self.ErgodicMode._CacheData.get(iid)
        if IDData is None:# 尚未进入缓存
            if self.ErgodicMode._CacheIDNum<self.ErgodicMode.MaxIDCacheNum:# 当前缓存 ID 数小于最大缓存 ID 数，那么将该 ID 数据读入缓存
                self.ErgodicMode._CacheIDNum += 1
                IDData = self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=None, ids=[iid], dts=self.ErgodicMode._CacheDTs, args=args), factor_names=None, ids=[iid], dts=self.ErgodicMode._CacheDTs, args=args).iloc[:, :, 0]
                self.ErgodicMode._CacheData[iid] = IDData
                self.ErgodicMode._Queue2SubProcess.put((None, (iid, None)))
            else:# 当前缓存 ID 数等于最大缓存 ID 数，那么将检查最小读取次数的 ID
                CacheIDReadNum = self.ErgodicMode._IDReadNum[self.ErgodicMode._CacheData.keys()]
                MinReadNumInd = CacheIDReadNum.argmin()
                if CacheIDReadNum.loc[MinReadNumInd]<self.ErgodicMode._IDReadNum[iid]:# 当前读取的 ID 的读取次数超过了缓存 ID 读取次数的最小值，缓存该 ID 数据
                    IDData = self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=None, ids=[iid], dts=self.ErgodicMode._CacheDTs, args=args), factor_names=None, ids=[iid], dts=self.ErgodicMode._CacheDTs, args=args).iloc[:, :, 0]
                    PopID = MinReadNumInd
                    self.ErgodicMode._CacheData.pop(PopID)
                    self.ErgodicMode._CacheData[iid] = IDData
                    self.ErgodicMode._Queue2SubProcess.put((None,(iid, PopID)))
                else:# 当前读取的 ID 的读取次数没有超过缓存 ID 读取次数的最小值, 放弃缓存该 ID 数据
                    return self.__QS_calcData__(raw_data=self.__QS_prepareRawData__(factor_names=factor_names, ids=[iid], dts=dts, args=args), factor_names=factor_names, ids=[iid], dts=dts, args=args).iloc[:, :, 0]
        if factor_names is not None: IDData = IDData.ix[:, factor_names]
        if dts is not None: IDData = IDData.ix[dts, :]
        return IDData
    def _readData_ErgodicMode(self, factor_names=None, ids=None, dts=None, args={}):
        if self.ErgodicMode.CacheMode=="因子": return self._readData_FactorCacheMode(factor_names=factor_names, ids=ids, dts=dts, args=args)
        if ids is None: ids = self._IDs
        return pd.Panel({iID: self._readIDData(iID, factor_names=factor_names, dts=dts, args=args) for iID in ids}).swapaxes(0, 2)
    # 启动遍历模式, dts: 遍历的时间点序列或者迭代器
    def start(self, dts=None, ids=None, **kwargs):
        if self.ErgodicMode._isStarted: return 0
        self.ErgodicMode._DateTimes = np.array(self.getDateTime(), dtype="O")
        self.ErgodicMode._IDs = (self.getID() if ids is None else ids)
        self.ErgodicMode._CurInd = -1# 当前时点在 dts 中的位置, 以此作为缓冲数据的依据
        self.ErgodicMode._DTNum = self.ErgodicMode._DateTimes.shape[0]# 时点数
        self.ErgodicMode._CacheDTs = []# 缓冲的时点序列
        self.ErgodicMode._CacheData = {}# 当前缓冲区
        self.ErgodicMode._CacheFactorNum = 0# 当前缓存因子个数, 小于等于 self.MaxFactorCacheNum
        self.ErgodicMode._CacheIDNum = 0# 当前缓存ID个数, 小于等于 self.MaxIDCacheNum
        self.ErgodicMode._FactorReadNum = pd.Series(0, index=self.FactorNames)# 因子读取次数, pd.Series(读取次数, index=self.FactorNames)
        self.ErgodicMode._IDReadNum = pd.Series()# ID读取次数, pd.Series(读取次数, index=self.FactorNames)
        self.ErgodicMode._Queue2SubProcess = Queue()# 主进程向数据准备子进程发送消息的管道
        self.ErgodicMode._Queue2MainProcess = Queue()# 数据准备子进程向主进程发送消息的管道
        self.ErgodicMode._TagName = (str(uuid.uuid1()) if os.name=="nt" else None)# 共享内存的 tag
        if self.ErgodicMode.CacheMode=="因子": self.ErgodicMode._CacheDataProcess = Process(target=_prepareMMAPFactorCacheData, args=(self, ), daemon=True)
        else: self.ErgodicMode._CacheDataProcess = Process(target=_prepareMMAPIDCacheData, args=(self, ), daemon=True)
        self.ErgodicMode._CacheDataProcess.start()
        self.ErgodicMode._isStarted = True
        return 0
    # 时间点向前移动, idt: 时间点, datetime.dateime
    def move(self, idt, *args, **kwargs):
        PreInd = self.ErgodicMode._CurInd
        self.ErgodicMode._CurInd = PreInd + np.sum(self.ErgodicMode._DateTimes[PreInd+1:]<=idt)
        if (self.ErgodicMode._CurInd>-1) and ((not self.ErgodicMode._CacheDTs) or (self.ErgodicMode._DateTimes[self.ErgodicMode._CurInd]>self.ErgodicMode._CacheDTs[-1])):# 需要读入缓冲区的数据
            self.ErgodicMode._Queue2SubProcess.put((None, None))
            DataLen, Msg = self.ErgodicMode._Queue2MainProcess.get()
            if os.name=="nt": MMAPCacheData = mmap.mmap(-1, DataLen, tagname=self.ErgodicMode._TagName)# 当前共享内存缓冲区
            else: MMAPCacheData, Msg = Msg, None
            if self.ErgodicMode._CurInd==PreInd+1:# 没有跳跃, 连续型遍历
                self.ErgodicMode._Queue2SubProcess.put((self.ErgodicMode._CurInd, None))
                self.ErgodicMode._CacheDTs = self.ErgodicMode._DateTimes[max((0, self.ErgodicMode._CurInd-self.ErgodicMode.BackwardPeriod)):min((self.ErgodicMode._DTNum, self.ErgodicMode._CurInd+self.ErgodicMode.ForwardPeriod+1))].tolist()
            else:# 出现了跳跃
                LastCacheInd = (self.ErgodicMode._DateTimes.searchsorted(self.ErgodicMode._CacheDTs[-1]) if self.ErgodicMode._CacheDTs else self.ErgodicMode._CurInd-1)
                self.ErgodicMode._Queue2SubProcess.put((LastCacheInd+1, None))
                self.ErgodicMode._CacheDTs = self.ErgodicMode._DateTimes[max((0, LastCacheInd+1-self.ErgodicMode.BackwardPeriod)):min((self.ErgodicMode._DTNum, LastCacheInd+1+self.ErgodicMode.ForwardPeriod+1))].tolist()
            MMAPCacheData.seek(0)
            self.ErgodicMode._CacheData = pickle.loads(MMAPCacheData.read(DataLen))
    # 结束遍历模式
    def end(self):
        if not self.ErgodicMode._isStarted: return 0
        self.ErgodicMode._CacheData, self.ErgodicMode._FactorReadNum, self.ErgodicMode._IDReadNum = None, None, None
        self.ErgodicMode._Queue2SubProcess.put(None)
        self.ErgodicMode._CacheDataProcess = None
        self.ErgodicMode._isStarted = False
        return 0
    # ------------------------------------运算模式------------------------------------
    # 获取因子表准备原始数据的分组信息, [(因子表对象, [因子名], [原始因子名], [时点], {参数})]
    def __QS_genGroupInfo__(self, factors, operation_mode):
        StartDT = dt.datetime.now()
        FactorNames, RawFactorNames = [], set()
        for iFactor in factors:
            FactorNames.append(iFactor.Name)
            RawFactorNames.add(iFactor._NameInFT)
            StartDT = min((StartDT, operation_mode._FactorStartDT[iFactor.Name]))
        EndDT = operation_mode.DateTimes[-1]
        StartInd, EndInd = operation_mode.DTRuler.index(StartDT), operation_mode.DTRuler.index(EndDT)
        return [(self, FactorNames, list(RawFactorNames), operation_mode.DTRuler[StartInd:EndInd+1], {})]
    def __QS_saveRawData__(self, raw_data, factor_names, raw_data_dir, pid_ids, file_name):
        if 'ID' in raw_data:# 如果原始数据有 ID 列，按照 ID 列划分后存入子进程的原始文件中
            raw_data = raw_data.set_index(['ID'])
            CommonCols = list(raw_data.columns.difference(set(factor_names)))
            AllIDs = set(raw_data.index)
            for iPID, iIDs in pid_ids.items():
                with shelve.open(raw_data_dir+os.sep+iPID+os.sep+file_name) as iFile:
                    iIDs = sorted(AllIDs.intersection(set(iIDs)))
                    iData = raw_data.loc[iIDs]
                    for jFactorName in factor_names: iFile[jFactorName] = iData[CommonCols+[jFactorName]].reset_index()
        else:# 如果原始数据没有 ID 列，则将所有数据分别存入子进程的原始文件中
            for iPID in pid_ids:
                with shelve.open(raw_data_dir+os.sep+iPID+os.sep+file_name) as iFile:
                    iFile["RawData"] = raw_data
        return 0
    def _genFactorDict(self, factors, factor_dict={}):
        for iFactor in factors:
            iFactor._QS_OperationMode = self.OperationMode
            if (not isinstance(iFactor.Name, str)) or (iFactor.Name=="") or (iFactor is not factor_dict.get(iFactor.Name, iFactor)):# 该因子命名错误或者未命名, 或者有因子重名
                iFactor.Name = genAvailableName("TempFactor", factor_dict)
            factor_dict[iFactor.Name] = iFactor
            factor_dict.update(self._genFactorDict(getattr(iFactor, "Descriptors", []), factor_dict))
        return factor_dict
    def _initOperation(self):
        # 检查时点, ID 序列的合法性
        if not self.OperationMode.DateTimes: raise __QS_Error__("运算时点序列不能为空!")
        if not self.OperationMode.IDs: raise __QS_Error__("运算 ID 序列不能为空!")
        # 检查时点标尺是否合适
        DTs = pd.Series(np.arange(0, len(self.OperationMode.DTRuler)), index=list(self.OperationMode.DTRuler)).ix[self.OperationMode.DateTimes]
        if pd.isnull(DTs).sum()>0: raise __QS_Error__("运算时点序列超出了时点标尺!")
        elif (DTs.diff().iloc[1:]!=1).sum()>0: raise __QS_Error__("运算时点序列的频率与时点标尺不一致!")
        # 检查因子的合法性, 解析出所有的因子(衍生因子所依赖的描述子也在内)
        if not self.OperationMode.FactorNames: self.OperationMode.FactorNames = self.FactorNames
        self.OperationMode._Factors = []# 因子列表
        self.OperationMode._FactorDict = {}# 因子字典, {因子名:因子}, 包括所有的因子, 即衍生因子所依赖的描述子也在内
        for iFactorName in self.OperationMode.FactorNames:
            iFactor = self.getFactor(iFactorName)
            iFactor._QS_OperationMode = self.OperationMode
            self.OperationMode._Factors.append(iFactor)
            self.OperationMode._FactorDict[iFactorName] = iFactor
        self.OperationMode._FactorDict = self._genFactorDict(self.OperationMode._Factors, self.OperationMode._FactorDict)
        # 生成原始数据和缓存数据存储目录
        with __QS_CacheLock__:
            self.OperationMode._CacheDir = __QS_CachePath__ + os.sep + genAvailableName("FT", listDirDir(__QS_CachePath__))
            os.mkdir(self.OperationMode._CacheDir)
            self.OperationMode._RawDataDir = self.OperationMode._CacheDir+os.sep+'RawData'# 原始数据存放根目录
            self.OperationMode._CacheDataDir = self.OperationMode._CacheDir+os.sep+'CacheData'# 中间数据存放根目录
            os.mkdir(self.OperationMode._RawDataDir)
            os.mkdir(self.OperationMode._CacheDataDir)
        if self.OperationMode.SubProcessNum==0:# 串行模式
            self.OperationMode._PIDs = ["0"]
            self.OperationMode._PID_IDs = {"0":self.OperationMode.IDs}
            os.mkdir(self.OperationMode._RawDataDir+os.sep+"0")
            os.mkdir(self.OperationMode._CacheDataDir+os.sep+"0")
            self.OperationMode._PID_Lock = {"0":Lock()}
        else:
            self.OperationMode._PIDs = []
            self.OperationMode._PID_IDs = {}
            nPrcs = min((self.OperationMode.SubProcessNum, len(self.OperationMode.IDs)))
            SubIDs = partitionList(self.OperationMode.IDs, nPrcs)
            self.OperationMode._PID_Lock = {}
            for i in range(nPrcs):
                iPID = "0-"+str(i)
                self.OperationMode._PIDs.append(iPID)
                self.OperationMode._PID_IDs[iPID] = SubIDs[i]
                os.mkdir(self.OperationMode._RawDataDir+os.sep+iPID)
                os.mkdir(self.OperationMode._CacheDataDir+os.sep+iPID)
                self.OperationMode._PID_Lock[iPID] = Lock()
        # 创建用于多进程的 Event 数据
        self.OperationMode._Event = {}# {因子名: (Sub2MainQueue, Event)}
        # 给每个因子设置运算模式参数对象
        for iFactor in self.OperationMode._FactorDict.values(): iFactor._OperationMode = self.OperationMode
        # 生成所有因子的起始时点信息
        self.OperationMode._FactorStartDT = {}# {因子名: 起始时点}
        for iFactor in self.OperationMode._FactorDict.values(): iFactor._QS_updateStartDT(self.OperationMode.DateTimes[0], self.OperationMode._FactorStartDT)
    def _prepareRawData(self):
        self._initOperation()
        print("1. 原始数据准备", end="")
        StartT = time.process_time()
        # 分组准备数据
        FTs, FT_Factors = {}, {}# {id(因子表) : 因子表}, {id(因子表) : [因子]}
        for iFactor in self.OperationMode._FactorDict.values():
            if iFactor.FactorTable is not None:
                iFTID = id(iFactor.FactorTable)
                iFactorList = FT_Factors.setdefault(iFTID, [])
                iFactorList.append(iFactor)
                FTs[iFTID] = iFactor.FactorTable
        GroupInfo, RawDataFileNames = [], []#[(因子表对象, [因子名], [原始因子名], [时点], {参数})], [原始数据文件名]
        for iFTID, iFT in FTs.items():
            iGroupInfo = iFT.__QS_genGroupInfo__(FT_Factors[iFTID], self.OperationMode)
            iRawDataFileNames = ["-".join((iFT.Name, str(iFTID), str(i))) for i in range(len(iGroupInfo))]
            GroupInfo.extend(iGroupInfo)
            RawDataFileNames.extend(iRawDataFileNames)
            for j in range(len(iGroupInfo)):
                for kFactorName in iGroupInfo[j][1]:
                    self.OperationMode._FactorDict[kFactorName]._RawDataFile = iRawDataFileNames[j]
        args = {"GroupInfo":GroupInfo, "FT":self, "RawDataFileNames":RawDataFileNames}
        if self.OperationMode.SubProcessNum==0:
            Error = _prepareRawData(args)
        else:
            nPrcs = min((self.OperationMode.SubProcessNum, len(args["GroupInfo"])))
            Procs,Main2SubQueue,Sub2MainQueue = startMultiProcess(pid="0", n_prc=nPrcs, target_fun=_prepareRawData,
                                                                  arg=args, partition_arg=["GroupInfo", "RawDataFileNames"],
                                                                  n_partition_head=0, n_partition_tail=0,
                                                                  main2sub_queue="None", sub2main_queue="Single")
            for i in tqdm(range(len(GroupInfo))):
                iPID, Error, iMsg = Sub2MainQueue.get()
                if Error!=1:
                    for iPID, iProc in Procs.items():
                        if iProc.is_alive(): iProc.terminate()
                    raise __QS_Error__(iMsg)
            for iPrcs in Procs.values(): iPrcs.join()
        print("耗时 : %.2f" % (time.process_time()-StartT, ))
    def calculate(self, factor_db, table_name, if_exists="append", **kwargs):
        self._prepareRawData()
        print("2. 因子数据计算", end="")
        StartT = time.process_time()
        Args = {"FT":self, "PID":"0", "FactorDB":factor_db, "TableName":table_name, "if_exists":if_exists}
        if self.OperationMode.SubProcessNum==0:
            _calculate(Args)
        else:
            nPrcs = len(self.OperationMode._PIDs)
            nTask = len(self.OperationMode._Factors) * nPrcs
            EventState = {iFactorName:0 for iFactorName in self.OperationMode._Event}
            Procs, Main2SubQueue, Sub2MainQueue = startMultiProcess(pid="0", n_prc=nPrcs, target_fun=_calculate, arg=Args,
                                                                    main2sub_queue="None", sub2main_queue="Single")
            iProg = 0
            with ProgressBar(max_value=nTask) as ProgBar:
                while True:
                    nEvent = len(EventState)
                    if nEvent>0:
                        FactorNames = tuple(EventState.keys())
                        for iFactorName in FactorNames:
                            iQueue = self.OperationMode._Event[iFactorName][0]
                            while not iQueue.empty():
                                jInc = iQueue.get()
                                EventState[iFactorName] += jInc
                            if EventState[iFactorName]>=nPrcs:
                                self.OperationMode._Event[iFactorName][1].set()
                                EventState.pop(iFactorName)
                    while ((not Sub2MainQueue.empty()) or (nEvent==0)) and (iProg<nTask):
                        iPID, iErrorCode, iMsg = Sub2MainQueue.get()
                        if iErrorCode==-1:
                            for iProc in Procs:
                                if iProc.is_alive(): iProc.terminate()
                            raise __QS_Error__('进程 '+iPID+' :运行失败:'+str(iMsg))
                        else:
                            iProg += 1
                            ProgBar.update(iProg)
                    if iProg>=nTask: break
            for iPID, iPrcs in Procs.items(): iPrcs.join()
        print("耗时 : %.2f" % (time.process_time()-StartT))
        # 清理缓存
        #try:
            #shutil.rmtree(self.OperationMode._CacheDir)
        #except:
            #print("警告 : 缓存文件夹 : '%s' 清除失败!" % self.OperationMode._CacheDir)
        return 0

# 自定义因子表
class CustomFT(FactorTable):
    """自定义因子表"""
    def __init__(self, name, sys_args={}, **kwargs):
        self._DateTimes = []# 数据源可提取的最长时点序列，[datetime.datetime]
        self._IDs = []# 数据源可提取的最长ID序列，['600000.SH']
        self._FactorDict = pd.DataFrame(columns=["FTID", "ArgIndex", "NameInFT", "DataType"], dtype=np.dtype("O"))# 数据源中因子的来源信息
        self._TableArgDict = {}# 数据源中的表和参数信息, {id(FT) : (FT, [args]), id(None) : ([Factor], [args])}
        self._IDFilterStr = None# ID 过滤条件字符串, "@收益率>0", 给定日期, 数据源的 getID 将返回过滤后的 ID 序列
        self._CompiledIDFilter = {}# 编译过的过滤条件字符串以及对应的因子列表, {条件字符串: (编译后的条件字符串,[因子])}
        self._isStarted = False# 数据源是否启动
        return super().__init__(name=name, fdb=None, sys_args=sys_args, **kwargs)
    @property
    def FactorNames(self):
        return self._FactorDict.index.tolist()
    def getFactorMetaData(self, factor_names=None, key=None):
        if factor_names is None:
            factor_names = self.FactorNames
        if key=="DataType":
            return self._FactorDict["DataType"].ix[factor_names]
        MetaData = {}
        for iFactorName in factor_names:
            iFTID = self._FactorDict.loc[iFactorName, "FTID"]
            iArgIndex = int(self._FactorDict.loc[iFactorName, "ArgIndex"])
            if iFTID==id(None):
                iFactor = self._TableArgDict[iFTID][0][iArgIndex]
                MetaData[iFactorName] = iFactor.getMetaData(key=key)
            else:
                iFT = self._TableArgDict[iFTID][0]
                iNameInFT = self._FactorDict["NameInFT"].loc[iFactorName]
                MetaData[iFactorName] = FT.getFactorMetaData(factor_names=[iNameInFT], key=key).ix[iNameInFT]
        if key is None:
            return pd.DataFrame(MetaData)
        else:
            return pd.Series(MetaData)
    def getFactor(self, ifactor_name, args={}):
        iFTID = self._FactorDict.loc[ifactor_name, "FTID"]
        iArgIndex = int(self._FactorDict.loc[ifactor_name, "ArgIndex"])
        if iFTID==id(None):
            return self._TableArgDict[iFTID][0][iArgIndex]
        else:
            iFT = self._TableArgDict[iFTID][0]
            iNameInFT = self._FactorDict["NameInFT"].loc[ifactor_name]
            iFactor = iFT.getFactor(ifactor_name=iNameInFT, args=args)
            iFactor.Name = ifactor_name
            return iFactor
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        DateTimes = self._DateTimes
        if start_dt is not None:
            DateTimes = DateTimes[DateTimes>=start_dt]
        if end_dt is not None:
            DateTimes = DateTimes[DateTimes<=end_dt]
        return DateTimes
    def getID(self, ifactor_name=None, idt=None, args={}):
        return self._IDs
    def getIDMask(self, idt, ids=None, id_filter_str=None, args={}):
        if ids is None: ids = self.getID(idt=idt, args=args)
        OldIDFilterStr = self.setIDFilter(id_filter_str)
        if self._IDFilterStr is None:
            self._IDFilterStr = OldIDFilterStr
            return pd.Series(True, index=ids)
        CompiledFilterStr, IDFilterFactors = self._CompiledIDFilter[self._IDFilterStr]
        temp = self.readData(factor_names=IDFilterFactors, ids=ids, dts=[idt], args=args).loc[:, idt, :]
        self._IDFilterStr = OldIDFilterStr
        return eval(CompiledFilterStr)
    def getFilteredID(self, idt, id_filter_str=None, args={}):
        if not id_filter_str: return self.getID(idt=idt, args=args)
        CompiledFilterStr, IDFilterFactors = self._CompiledIDFilter[self._IDFilterStr]
        if CompiledIDFilterStr is None: raise __QS_Error__("过滤条件字符串有误!")
        temp = self.readData(factor_names=IDFilterFactors, ids=ids, dts=[idt], args=args).loc[:, idt, :]
        self._IDFilterStr = OldIDFilterStr
        return eval("temp["+CompiledIDFilterStr+"].index.tolist()")
    def __QS_calcData__(self, raw_data, factor_names=None, ids=None, dts=None, args={}):
        if factor_names is None: factor_names = self.FactorNames
        if dts is None: dts = self._DateTimes
        if ids is None: ids = self._IDs
        Data = {}
        TableArgFactor = self._FactorDict.loc[factor_names].groupby(by=["FTID", "ArgIndex"]).groups
        for iFTID, iArgIndex in TableArgFactor:
            if iFTID==id(None):
                iFactorList, iArgList = self._TableArgDict[iFTID]
                iFactor = iFactorList[iArgIndex]
                iArgs = iArgList[iArgIndex]
                Data[iFactor] = iFactor.readData(ids=ids, dts=dts, args=iArgs)
            else:
                iFT, iArgList = self._TableArgDict[iFTID]
                iArgs = iArgList[iArgIndex]
                iFactorNames = TableArgFactor[(iFTID, iArgIndex)]
                iNameInFT = self._FactorDict["NameInFT"].loc[iFactorNames].values.tolist()
                iData = iFT.readData(factor_names=iNameInFT, ids=ids, dts=dts, args=iArgs)
                iData.items = iFactorNames
                Data.update(dict(iData))
        return pd.Panel(Data).loc[factor_names, :, :]
    # ---------------新的接口------------------
    # 添加因子, factor_list: 因子对象列表
    def addFactors(self, factor_list=[], factor_table=None, factor_names=None, args={}):
        for iFactor in factor_list:
            if iFactor.Name in self._FactorDict.index: raise __QS_Error__("因子: '%s' 有重名!" % iFactor.Name)
            iFT = iFactor.FactorTable
            iFTID = id(iFT)
            iDataType = iFactor.getMetaData(key="DataType")
            if iFT is None:
                iFactorList, iArgList = self._TableArgDict.get(iFTID, ([], []))
                self._FactorDict.loc[iFactor.Name] = (iFTID, len(iArgList), None, iDataType)
                iFactorList.append(iFactor)
                iArgList.append(args)
                self._TableArgDict[iFTID] = (iFactorList, iArgList)
            else:
                iFT, iArgList = self._TableArgDict.get(iFTID, (iFT, []))
                iArgIndex = (len(iArgList) if args not in iArgList else iArgList.index(args))
                self._FactorDict.loc[iFactor.Name] = (iFTID, iArgIndex, iFactor._NameInFT, iDataType)
                iArgList.append(args)
                self._TableArgDict[iFTID] = (iFT, iArgList)
        if factor_table is None: return 0
        if factor_names is None: factor_names = factor_table.FactorNames
        iFTID = id(factor_table)
        factor_table, iArgList = self._TableArgDict.get(iFTID, (factor_table, []))
        if args in iArgList:
            iArgIndex = iArgList.index(args)
        else:
            iArgIndex = len(iArgList)
            iArgList.append(args)
        DataTypes = factor_table.getFactorMetaData(factor_names, key="DataType")
        for iFactorName in factor_names:
            if iFactorName not in factor_table.FactorNames: raise __QS_Error__("指定的因子: '%s' 不存在!" % iFactorName)
            if iFactorName in self._FactorDict.index: raise __QS_Error__("因子: '%s' 有重名!" % iFactorName)
            iDataType = DataTypes[iFactorName]
            self._FactorDict.loc[iFactorName] = (iFTID, iArgIndex, iFactorName, iDataType)
        self._TableArgDict[iFTID] = (factor_table, iArgList)
        self._FactorDict["ArgIndex"] = self._FactorDict["ArgIndex"].astype(np.int64)
        self._FactorDict["FTID"] = self._FactorDict["FTID"].astype(np.int64)
        return 0
    # 删除因子, factor_names = None 表示删除所有因子
    def deleteFactors(self, factor_names=None):
        if factor_names is None:
            factor_names = self.FactorNames
        for iFactorName in factor_names:
            if iFactorName not in self._FactorDict.index:
                continue
            iFTID = self._FactorDict.loc[iFactorName, "FTID"]
            iArgIndex = int(self._FactorDict.loc[iFactorName, "ArgIndex"])
            if iFTID==id(None):
                iFactorList, iArgList = self._TableArgDict[iFTID]
                iFactorList.pop(iArgIndex)
                iArgList.pop(iArgIndex)
            else:
                iFT, iArgList = self._TableArgDict[iFTID]
                iArgList.pop(iArgIndex)
            if not iArgList:
                self._TableArgDict.pop(iFTID)
        self._FactorDict = self._FactorDict.loc[sorted(set(self._FactorDict.index).difference(set(factor_names)))]
        return 0
    # 重命名因子
    def renameFactor(self, factor_name, new_factor_name):
        if factor_name not in self._FactorDict.index: raise __QS_Error__("因子: '%s' 不存在!" % factor_name)
        if (new_factor_name!=factor_name) and (new_factor_name in self._FactorDict.index): raise __QS_Error__("因子: '%s' 有重名!" % new_factor_name)
        FactorNames = list(self._FactorDict.index)
        FactorNames[FactorNames.index(factor_name)] = new_factor_name
        self._FactorDict.index = FactorNames
        return 0
    # 设置时间点序列
    def setDateTime(self, dts):
        self._DateTimes = sorted(dts)
    # 设置 ID 序列
    def setID(self, ids):
        self._IDs = sorted(ids)
    # ID 过滤条件
    @property
    def IDFilterStr(self):
        return self._IDFilterStr
    # 设置 ID 过滤条件, id_filter_str, '@收益率$>0'
    def setIDFilter(self, id_filter_str):
        OldIDFilterStr = self._IDFilterStr
        if id_filter_str is None:
            self._IDFilterStr = None
            return OldIDFilterStr
        elif (not isinstance(id_filter_str, str)) or (id_filter_str==""):
            raise __QS_Error__("条件字符串必须为非空字符串或者 None!")
        CompiledIDFilter = self._CompiledIDFilter.get(id_filter_str, None)
        if CompiledIDFilter is not None:# 该条件已经编译过
            self._IDFilterStr = id_filter_str
            return OldIDFilterStr
        CompiledIDFilterStr, IDFilterFactors = testIDFilterStr(id_filter_str, self.FactorNames)
        if CompiledIDFilterStr is None:
            raise __QS_Error__("条件字符串有误!")
        self._IDFilterStr = id_filter_str
        self._CompiledIDFilter[id_filter_str] = (CompiledIDFilterStr, IDFilterFactors)
        return OldIDFilterStr


# ---------- 内置的因子运算----------
# 将运算结果转换成真正的可以存储的因子
def Factorize(factor_object, factor_name, args={}):
    factor_object.Name = factor_name
    for iArg, iVal in args.items(): factor_object[iArg] = iVal
    return factor_object
def _UnitaryOperator(f, idt, iid, x, args):
    Fun = args.get("Fun", None)
    if Fun is not None: Data = Fun(f, idt, iid, x, args["Arg"])
    else: Data = x[0]
    OperatorType = args.get("OperatorType", "neg")
    if OperatorType=="neg": return -Data
    elif OperatorType=="abs": return np.abs(Data)
    elif OperatorType=="not": return (~Data)
    return Data
def _BinaryOperator(f, idt, iid, x, args):
    Fun1 = args.get("Fun1", None)
    if Fun1 is not None:
        Data1 = Fun1(f, idt, iid, x[:args["SepInd"]], args["Arg1"])
    else:
        Data1 = args.get("Data1", None)
        if Data1 is None: Data1 = x[0]
    Fun2 = args.get("Fun2",None)
    if Fun2 is not None:
        Data2 = Fun2(f, idt, iid, x[args["SepInd"]:], args["Arg2"])
    else:
        Data2 = args.get("Data2", None)
        if Data2 is None: Data2 = x[args["SepInd"]]
    OperatorType = args.get("OperatorType", "add")
    if OperatorType=="add": return Data1 + Data2
    elif OperatorType=="sub": return Data1 - Data2
    elif OperatorType=="mul": return Data1 * Data2
    elif OperatorType=="div":
        if np.isscalar(Data2): return (Data1 / Data2 if Data2!=0 else np.empty(Data1.shape)+np.nan)
        Data2[Data2==0] = np.nan
        return Data1/Data2
    elif OperatorType=="floordiv": return Data1 // Data2
    elif OperatorType=="mod": return Data1 % Data2
    elif OperatorType=="pow":
        if np.isscalar(Data2):
            if Data2<0: Data1[Data1==0] = np.nan
            return Data1 ** Data2
        if np.isscalar(Data1):
            if Data1==0: Data2[Data2<0] = np.nan
            return Data1 ** Data2
        Data1[(Data1==0) & (Data2<0)] = np.nan
        return Data1 ** Data2
    elif OperatorType=="and": return (Data1 & Data2)
    elif OperatorType=="or": return (Data1 | Data2)
    elif OperatorType=="xor": return (Data1 ^ Data2)
    elif OperatorType=="<": return (Data1 < Data2)
    elif OperatorType=="<=": return (Data1 <= Data2)
    elif OperatorType==">": return (Data1 > Data2)
    elif OperatorType==">=": return (Data1 >= Data2)
    elif OperatorType=="==": return (Data1 == Data2)
    elif OperatorType=="!=": return (Data1 != Data2)

# 因子
# 因子可看做一个 DataFrame(index=[时间点], columns=[ID])
# 时间点数据类型是 datetime.datetime, ID 的数据类型是 str
# 不支持某个操作时, 方法产生错误
# 没有相关数据时, 方法返回 None
class Factor(__QS_Object__):
    Name = Str("因子")
    def __init__(self, name, ft=None, sys_args={}, **kwargs):
        self._FactorTable = ft# 因子所属的因子表, None 表示独立的衍生因子
        self._NameInFT = name# 因子在所属的因子表中的名字
        self.Name = name# 因子对外显示的名称
        self._OperationMode = None# 运算模式对象
        self._RawDataFile = ""# 原始数据存放地址
        self._isCacheDataOK = False
        return super().__init__(sys_args=sys_args, **kwargs)
    @property
    def FactorTable(self):
        return self._FactorTable
    @property
    def DTRuler(self):
        if self._OperationMode is None: return None
        return self._OperationMode.DTRuler
    # 获取因子的元数据
    def getMetaData(self, key=None):
        if self._FactorTable is not None: return self._FactorTable.getFactorMetaData(factor_names=[self._NameInFT], key=key).loc[self._NameInFT]
        if key is None: return pd.Series(dtype="O")
        return None
    # 获取 ID 序列
    def getID(self, idt=None, args={}):
        if self._OperationMode is not None: return self._OperationMode.IDs
        if self._FactorTable is not None: return self._FactorTable.getID(ifactor_name=self._NameInFT, idt=idt, args=args)
        return []
    # 获取时间点序列
    def getDateTime(self, iid=None, start_dt=None, end_dt=None, args={}):
        if self._OperationMode is not None: return self._OperationMode.DateTimes
        if self._FactorTable is not None: return self._FactorTable.getDateTime(ifactor_name=self._NameInFT, iid=iid, start_dt=start_dt, end_dt=end_dt, args=args)
        return []
    # --------------------------------数据读取---------------------------------
    # 读取数据, 返回: Panel(item=[因子], major_axis=[时间点], minor_axis=[ID])
    def readData(self, ids=None, dts=None):
        if self.FactorTable is not None: return self._FactorTable.readData(factor_names=[self._NameInFT], ids=ids, dts=dts, args=self.Args).loc[self._NameInFT]
        return None
    # ------------------------------------运算模式------------------------------------
    # 获取数据的开始时点, start_dt:新起始时点, dt_dict: 当前所有因子的时点信息: {因子名 : 开始时点}
    def _QS_updateStartDT(self, start_dt, dt_dict):
        OldStartDT = dt_dict.get(self.Name, start_dt)
        dt_dict[self.Name] = (start_dt if start_dt<OldStartDT else OldStartDT)
    # 准备缓存数据
    def __QS_prepareCacheData__(self):
        StartDT = self._OperationMode._FactorStartDT[self.Name]
        EndDT = self._OperationMode.DateTimes[-1]
        StartInd, EndInd = self._OperationMode.DTRuler.index(StartDT), self._OperationMode.DTRuler.index(EndDT)
        DTs = self._OperationMode.DTRuler[StartInd:EndInd+1]
        RawDataFilePath = self._OperationMode._RawDataDir+os.sep+self._OperationMode._iPID+os.sep+self._RawDataFile
        if  os.path.isfile(RawDataFilePath+self._OperationMode._FileSuffix):
            with shelve.open(RawDataFilePath, "r") as File:
                RawData = File[self._NameInFT]
            StdData = self._FactorTable.__QS_calcData__(RawData, factor_names=[self._NameInFT], ids=self._OperationMode._PID_IDs[self._OperationMode._iPID], dts=DTs, args=self.Args).iloc[0]
        else:
            StdData = self._FactorTable.readData(factor_names=[self._NameInFT], ids=self._OperationMode._PID_IDs[self._OperationMode._iPID], dts=DTs, args=self.Args).iloc[0]
        with self._OperationMode._PID_Lock[self._OperationMode._iPID]:
            with shelve.open(self._OperationMode._CacheDataDir+os.sep+self._OperationMode._iPID+os.sep+self.Name) as CacheFile:
                CacheFile["StdData"] = StdData
        self._isCacheDataOK = True
        return StdData
    # 获取因子数据, pid=None表示取所有进程的数据
    def _QS_getData(self, dts, pids=None):
        pids = set(self._OperationMode._PID_IDs if pids is None else pids)
        if not self._isCacheDataOK:# 若没有准备好缓存数据, 准备缓存数据
            StdData = self.__QS_prepareCacheData__()
            if (StdData is not None) and (self._OperationMode._iPID in pids):
                pids.remove(self._OperationMode._iPID)
            else:
                StdData = None
        else:
            StdData = None
        while len(pids)>0:
            iPID = pids.pop()
            iFilePath = self._OperationMode._CacheDataDir+os.sep+iPID+os.sep+self.Name
            if not os.path.isfile(iFilePath+self._OperationMode._FileSuffix):# 该进程的数据没有准备好
                pids.add(iPID)
                continue
            with self._OperationMode._PID_Lock[iPID]:
                with shelve.open(iFilePath, 'r') as CacheFile:
                    iStdData = CacheFile["StdData"]
            if StdData is None:
                StdData = iStdData
            else:
                StdData = pd.merge(StdData, iStdData, how='inner', left_index=True, right_index=True)
        StdData = StdData.loc[list(dts)].sort_index(axis=1)
        gc.collect()
        return StdData
    # -----------------------------重载运算符-------------------------------------
    def _genUnitaryOperatorInfo(self):
        if (self.Name==""):# 因子为中间运算因子
            Args = {"Fun":self.Operator, "Arg":self.ModelArgs}
            return (self.Descriptors, Args)
        else:# 因子为正常因子
            return ([self], {})
    def _genBinaryOperatorInfo(self, other):
        if isinstance(other, Factor):# 两个因子运算
            if (self.Name=="") and (other.Name==""):# 两个因子因子名为空, 说明都是中间运算因子
                Args = {"Fun1":self.Operator, "Fun2":other.Operator, "SepInd":len(self.Descriptors), "Arg1":self.ModelArgs, "Arg2":other.ModelArgs}
                return (self.Descriptors+other.Descriptors, Args)
            elif (self.Name==""):# 第一个因子为中间运算因子
                Args = {"Fun1":self.Operator, "SepInd":len(self.Descriptors), "Arg1":self.ModelArgs}
                return (self.Descriptors+[other], Args)
            elif (other.Name==""):# 第二个因子为中间运算因子
                Args = {"Fun2":other.SysArgs["算子"], "SepInd":1, "Arg2":other.ModelArgs}
                return ([self]+other.Descriptors, Args)
            else:# 两个因子均为正常因子
                Args = {"SepInd":1}
                return ([self, other], Args)
        elif (self.Name==""):# 中间运算因子+标量数据
            Args = {"Fun1":self.Operator, "SepInd":len(self.Descriptors), "Data2":other, "Arg1":self.ModelArgs}
            return (self.Descriptors, Args)
        else:# 正常因子+标量数据
            Args = {"SepInd":1, "Data2":other}
            return ([self], Args)
    def _genRBinaryOperatorInfo(self, other):
        if (self.Name==""):# 标量数据+中间运算因子
            Args = {"Fun2":self.Operator, "SepInd":0, "Data1":other, "Arg2":self.ModelArgs}
            return (self.Descriptors, Args)
        else:# 标量数据+正常因子
            Args = {"SepInd":0, "Data1":other}
            return ([self], Args)
    def __add__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "add"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __radd__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "add"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __sub__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "sub"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rsub__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "sub"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __mul__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors,Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "mul"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rmul__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "mul"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __pow__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "pow"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rpow__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "pow"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __truediv__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "div"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rtruediv__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "div"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __floordiv__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "floordiv"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rfloordiv__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "floordiv"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __mod__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "mod"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rmod__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "mod"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __and__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "and"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rand__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "and"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __or__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "or"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __ror__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "or"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __xor__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "xor"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __rxor__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genRBinaryOperatorInfo(other)
        Args["OperatorType"] = "xor"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __lt__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "<"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __le__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "<="
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __eq__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "=="
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __ne__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = "!="
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __gt__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = ">"
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __ge__(self, other):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genBinaryOperatorInfo(other)
        Args["OperatorType"] = ">="
        return PointOperation("", Descriptors, {"算子":_BinaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __neg__(self):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genUnitaryOperatorInfo()
        Args["OperatorType"] = "neg"
        return PointOperation("", Descriptors, {"算子":_UnitaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __pos__(self):
        return self
    def __abs__(self):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genUnitaryOperatorInfo()
        Args["OperatorType"] = "abs"
        return PointOperation("", Descriptors, {"算子":_UnitaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    def __invert__(self):
        from QuantStudio.FactorDataBase.FactorOperation import PointOperation
        Descriptors, Args = self._genUnitaryOperatorInfo()
        Args["OperatorType"] = "not"
        return PointOperation("", Descriptors, {"算子":_UnitaryOperator, "参数":Args, "运算时点":"多时点", "运算ID":"多ID"})
    
if __name__=='__main__':
    import time
    import datetime as dt
    
    # -----------测试遍历模式----------
    ## 创建因子数据库
    #from QuantStudio.FactorDataBase.HDF5DB import HDF5DB
    #MainDB = HDF5DB()
    #MainDB.connect()
    #FT = MainDB.getTable("ElementaryFactor")
    ## 创建自定义的因子表
    #MainFT = CustomFT("MainFT")
    #MainFT.addFactors(factor_table=FT, factor_names=["复权收盘价"], args={})
    #MainFT.setDateTime(FT.getDateTime(ifactor_name="复权收盘价", start_dt=dt.datetime(2014,1,1), end_dt=dt.datetime(2018,1,1)))
    #MainFT.setID(["000001.SZ", "600000.SH"])
    #MainFT.ErgodicMode.CacheMode = "ID"
    #StartT = time.process_time()
    #MainFT.start()
    #for iDateTime in MainFT.getDateTime():
        #MainFT.move(iDateTime)
        #iData = MainFT.readData(dts=[iDateTime]).iloc[:, 0, :]
        #print(iDateTime)
    #MainFT.end()
    #print(time.process_time()-StartT)
    
    # -----------测试因子运算模式----------
    from multiprocessing import cpu_count
    from QuantStudio.FactorDataBase.WindDB2 import WindDB2
    WDB = WindDB2()
    WDB.connect()
    FT = WDB.getTable("中国A股日行情")
    IDs = ["000001.SZ", "600000.SH"]# FT.getID(idt=dt.datetime(2018, 2, 1))
    DTs = FT.getDateTime(ifactor_name="开盘价", start_dt=dt.datetime(2018, 1, 1), end_dt=dt.datetime(2018, 2, 1))
    
    from QuantStudio.FactorDataBase.FactorTools import rolling_mean, standardizeZScore
    Low = FT.getFactor("最低价")
    High = FT.getFactor("最高价")
    #Mid = (Low + High)/2# 单点运算
    #Avg = rolling_mean(Low, window=2)# 时间序列运算
    Std = standardizeZScore(Low)# 截面运算
    
    #Data = Mid.readData(ids=IDs, dts=DTs)
    #Data = Avg.readData(ids=IDs, dts=DTs)
    Data = Std.readData(ids=IDs, dts=DTs)
    #Data0 = (Low.readData(ids=IDs, dts=DTs) + High.readData(ids=IDs, dts=DTs))/2
    #print(Data-Data0)
    # 运算模式
    #MainFT = CustomFT("MainFT")
    #MainFT.addFactors(factor_list=[Low, High, Sum])
    #MainFT.OperationMode.DateTimes = DTs
    #MainFT.OperationMode.IDs = IDs
    #MainFT.OperationMode.FactorNames = ["最低价", "最高价", "Sum"]
    #MainFT.OperationMode.SubProcessNum = 0
    #MainFT.OperationMode.DTRuler = MainFT.OperationMode.DateTimes
    
    #from QuantStudio.FactorDataBase.HDF5DB import HDF5DB
    #HDB = HDF5DB()
    #HDB.connect()    
    #MainFT.calculate(factor_db=HDB, table_name=genAvailableName("TestTable", HDB.TableNames), if_exists="append")
    
    #HDB.disconnect()
    WDB.disconnect()