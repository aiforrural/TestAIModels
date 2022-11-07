package server

import (
	"fmt"
	"github.com/wandb/wandb/nexus/service"
	"sync"

	log "github.com/sirupsen/logrus"
)

type Handler struct {
	handlerChan chan service.Record

	currentStep int64
	startTime   float64

	wg      *sync.WaitGroup
	writer  *Writer
	sender  *Sender
	fstream *FileStream
	run     service.RunRecord

	settings      *Settings
	respondResult func(result *service.Result)
}

func NewHandler(respondResult func(result *service.Result), settings *Settings) *Handler {
	wg := sync.WaitGroup{}
	writer := NewWriter(&wg, settings)
	sender := NewSender(&wg, respondResult, settings)
	handler := Handler{
		wg:            &wg,
		writer:        writer,
		sender:        sender,
		respondResult: respondResult,
		settings:      settings,
		handlerChan:   make(chan service.Record)}

	go handler.handlerGo()
	return &handler
}

func (handler *Handler) Stop() {
	close(handler.handlerChan)
}

func (h *Handler) startRunWorkers() {
	fsPath := fmt.Sprintf("%s/files/%s/%s/%s/file_stream",
		h.settings.BaseURL, h.run.Entity, h.run.Project, h.run.RunId)
	h.fstream = NewFileStream(h.wg, fsPath, h.settings)
}

func (handler *Handler) HandleRecord(rec *service.Record) {
	handler.handlerChan <- *rec
}

func (h *Handler) shutdownStream() {
	h.writer.Stop()
	h.sender.Stop()
	if h.fstream != nil {
		h.fstream.Stop()
	}
	h.wg.Wait()
}

func (h *Handler) captureRunInfo(run *service.RunRecord) {
	h.startTime = float64(run.StartTime.AsTime().UnixMicro()) / 1e6
	h.run = *run
}

func (h *Handler) handleRunStart(rec *service.Record, req *service.RunStartRequest) {
	h.captureRunInfo(req.Run)
	h.startRunWorkers()
}

func (h *Handler) handleRun(rec *service.Record, run *service.RunRecord) {
	// runResult := &service.RunUpdateResult{Run: run}

	// let sender take care of it
	h.sender.SendRecord(rec)

	/*
	   result := &service.Result{
	       ResultType: &service.Result_RunResult{runResult},
	       Control: rec.Control,
	       Uuid: rec.Uuid,
	   }
	   stream.respond <-*result
	*/
}

func (h *Handler) handleRunExit(rec *service.Record, runExit *service.RunExitRecord) {
	// TODO: need to flush stuff before responding with exit
	runExitResult := &service.RunExitResult{}
	result := &service.Result{
		ResultType: &service.Result_ExitResult{runExitResult},
		Control:    rec.Control,
		Uuid:       rec.Uuid,
	}
	h.respondResult(result)
	h.shutdownStream()
}

func (h *Handler) handleRequest(rec *service.Record, req *service.Request) {
	ref := req.ProtoReflect()
	desc := ref.Descriptor()
	num := ref.WhichOneof(desc.Oneofs().ByName("request_type")).Number()
	log.WithFields(log.Fields{"type": num}).Debug("PROCESS: REQUEST")

	switch x := req.RequestType.(type) {
	case *service.Request_PartialHistory:
		log.WithFields(log.Fields{"req": x}).Debug("PROCESS: got partial")
		h.handlePartialHistory(rec, x.PartialHistory)
	case *service.Request_RunStart:
		log.WithFields(log.Fields{"req": x}).Debug("PROCESS: got start")
		h.handleRunStart(rec, x.RunStart)
	default:
	}

	response := &service.Response{}
	result := &service.Result{
		ResultType: &service.Result_Response{response},
		Control:    rec.Control,
		Uuid:       rec.Uuid,
	}
	h.respondResult(result)
}

func (handler *Handler) handleRecord(msg *service.Record) {
	switch x := msg.RecordType.(type) {
	case *service.Record_Header:
		// fmt.Println("headgot:", x)
	case *service.Record_Request:
		log.WithFields(log.Fields{"req": x}).Debug("reqgot")
		handler.handleRequest(msg, x.Request)
	case *service.Record_Summary:
		// fmt.Println("sumgot:", x)
	case *service.Record_Run:
		// fmt.Println("rungot:", x)
		handler.handleRun(msg, x.Run)
	case *service.Record_History:
		// fmt.Println("histgot:", x)
	case *service.Record_Telemetry:
		// fmt.Println("telgot:", x)
	case *service.Record_OutputRaw:
		// fmt.Println("outgot:", x)
	case *service.Record_Exit:
		// fmt.Println("exitgot:", x)
		handler.handleRunExit(msg, x.Exit)
	case nil:
		// The field is not set.
		panic("bad2rec")
	default:
		bad := fmt.Sprintf("REC UNKNOWN type %T", x)
		panic(bad)
	}
}

func (h *Handler) storeRecord(msg *service.Record) {
	switch msg.RecordType.(type) {
	case *service.Record_Request:
		// dont log this
	case nil:
		// The field is not set.
		panic("bad3rec")
	default:
		h.writer.WriteRecord(msg)
	}
}

func (handler *Handler) handlerGo() {
	log.Debug("HANDLER")
	for {
		select {
		case record := <-handler.handlerChan:
			log.WithFields(log.Fields{"rec": record}).Debug("HANDLER")
			handler.storeRecord(&record)
			handler.handleRecord(&record)
		}
	}
	log.Debug("HANDLER OUT")
}